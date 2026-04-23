"""Tests for harness/api.py — FastAPI endpoints via TestClient."""
from fastapi.testclient import TestClient

from main import app


def test_health_returns_ok():
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_providers_returns_expected_set():
    with TestClient(app) as client:
        r = client.get("/providers")
        assert r.status_code == 200
        ids = {p["id"] for p in r.json()["providers"]}
        # Required minimum set; additional providers (like 'mock') are OK
        assert {"NONE", "ollama", "claude", "chatgpt", "gemini"} <= ids


def test_validate_chat_api_disables_state():
    with TestClient(app) as client:
        r = client.post("/validate", json={"row_config": {
            "framework": "langchain", "api": "chat",
            "stream": False, "state": False,
            "llm": "ollama", "mcp": "NONE", "routing": "via_agw",
        }})
        assert r.status_code == 200
        data = r.json()
        assert "state" in data["disabled_cells"]


def test_matrix_row_crud(tmp_data_dir, reset_api_state):
    """Create, update, delete matrix rows."""
    with TestClient(app) as client:
        # Empty matrix at start
        r = client.get("/matrix")
        assert r.status_code == 200
        initial_count = len(r.json()["rows"])

        # Create
        r = client.post("/matrix/row", json={
            "framework": "langchain", "api": "chat",
            "stream": False, "state": False,
            "llm": "ollama", "mcp": "NONE", "routing": "via_agw",
        })
        assert r.status_code == 200
        row_id = r.json()["row_id"]
        assert row_id

        # Read
        r = client.get("/matrix")
        assert len(r.json()["rows"]) == initial_count + 1

        # Update
        r = client.patch(f"/matrix/row/{row_id}", json={"stream": True})
        assert r.status_code == 200

        # Delete
        r = client.delete(f"/matrix/row/{row_id}")
        assert r.status_code == 200


def test_info_reports_adapter_discovery():
    with TestClient(app) as client:
        r = client.get("/info")
        assert r.status_code == 200
        data = r.json()
        assert "adapters" in data


# ── Plan B T12 — turn_plan_override + /templates/validate ──

def test_templates_validate_accepts_minimal_plan():
    from api import templates_validate
    out = templates_validate({"turn_plan": {"turns": [
        {"turn_id": "t0", "kind": "user_msg", "text": "hi"},
    ]}})
    assert out["ok"] is True
    assert out["errors"] == []


def test_templates_validate_rejects_missing_turn_id():
    from api import templates_validate
    out = templates_validate({"turn_plan": {"turns": [
        {"kind": "user_msg", "text": "hi"},
    ]}})
    assert out["ok"] is False
    assert any("turn_id" in e for e in out["errors"])


def test_templates_validate_rejects_unknown_kind():
    from api import templates_validate
    out = templates_validate({"turn_plan": {"turns": [
        {"turn_id": "t0", "kind": "wat", "text": "hi"},
    ]}})
    assert out["ok"] is False
    assert any("invalid kind" in e for e in out["errors"])


def test_templates_validate_force_state_ref_requires_lookback():
    from api import templates_validate
    out = templates_validate({"turn_plan": {"turns": [
        {"turn_id": "t0", "kind": "user_msg", "text": "hi"},
        {"turn_id": "t1", "kind": "force_state_ref"},
    ]}})
    assert out["ok"] is False
    assert any("lookback" in e for e in out["errors"])


def test_templates_validate_rejects_empty_turns():
    from api import templates_validate
    out = templates_validate({"turn_plan": {"turns": []}})
    assert out["ok"] is False
    assert any("non-empty" in e for e in out["errors"])


def test_matrix_row_turn_plan_override_roundtrip(tmp_data_dir, reset_api_state):
    """PATCH saves turn_plan_override; GET returns it; DELETE clears it."""
    with TestClient(app) as client:
        r = client.post("/matrix/row", json={
            "framework": "langchain", "api": "chat",
            "stream": False, "state": False,
            "llm": "ollama", "mcp": "NONE", "routing": "via_agw",
        })
        row_id = r.json()["row_id"]

        override = {"turns": [
            {"turn_id": "t0", "kind": "user_msg", "text": "override hi"},
        ]}
        r = client.patch(f"/matrix/row/{row_id}",
                         json={"turn_plan_override": override})
        assert r.status_code == 200

        r = client.get(f"/matrix/row/{row_id}")
        assert r.status_code == 200
        assert r.json().get("turn_plan_override") == override

        r = client.delete(f"/matrix/row/{row_id}/turn_plan_override")
        assert r.status_code == 200
        assert r.json()["ok"] is True

        r = client.get(f"/matrix/row/{row_id}")
        assert "turn_plan_override" not in r.json()


def test_matrix_row_clear_override_404_for_missing_row(tmp_data_dir, reset_api_state):
    with TestClient(app) as client:
        r = client.delete("/matrix/row/does-not-exist/turn_plan_override")
        assert r.status_code == 404


# ── Plan B T13 — clone-for-baseline row action ──

def test_clone_baseline_creates_direct_row(tmp_data_dir, reset_api_state):
    """POST /matrix/row/{id}/clone-baseline creates a sibling with routing=direct."""
    with TestClient(app) as client:
        # Create a source row (via_agw governed)
        r1 = client.post("/matrix/row", json={
            "framework": "langchain", "api": "chat",
            "stream": False, "state": False,
            "llm": "ollama", "mcp": "weather", "routing": "via_agw",
        })
        assert r1.status_code == 200
        src_id = r1.json()["row_id"]

        # Clone as baseline
        r2 = client.post(f"/matrix/row/{src_id}/clone-baseline")
        assert r2.status_code == 200
        new_id = r2.json()["row_id"]
        assert r2.json()["baseline_of"] == src_id
        assert new_id != src_id

        # Verify the new row exists with routing=direct + pairing metadata
        r3 = client.get(f"/matrix/row/{new_id}")
        assert r3.status_code == 200
        new_row = r3.json()
        assert new_row["routing"] == "direct"
        assert new_row["baseline_of"] == src_id
        assert new_row["framework"] == "langchain"
        assert new_row["llm"] == "ollama"
        assert new_row["mcp"] == "weather"
        assert "note" in new_row


def test_clone_baseline_carries_turn_plan_override(tmp_data_dir, reset_api_state):
    """Override on the source carries to the baseline so they run identical prompts."""
    with TestClient(app) as client:
        r1 = client.post("/matrix/row", json={
            "framework": "langchain", "api": "chat",
            "stream": False, "state": False,
            "llm": "ollama", "mcp": "NONE", "routing": "via_agw",
        })
        src_id = r1.json()["row_id"]

        custom_plan = {"turns": [
            {"turn_id": "t0", "kind": "user_msg", "text": "custom prompt"},
        ]}
        pr = client.patch(f"/matrix/row/{src_id}",
                          json={"turn_plan_override": custom_plan})
        assert pr.status_code == 200

        r2 = client.post(f"/matrix/row/{src_id}/clone-baseline")
        assert r2.status_code == 200
        new_id = r2.json()["row_id"]

        r3 = client.get(f"/matrix/row/{new_id}")
        assert r3.status_code == 200
        assert r3.json().get("turn_plan_override") == custom_plan


def test_clone_baseline_404_on_missing_row(tmp_data_dir, reset_api_state):
    with TestClient(app) as client:
        r = client.post("/matrix/row/row-nonexistent/clone-baseline")
        assert r.status_code == 404


def test_clone_baseline_400_on_already_direct_row(tmp_data_dir, reset_api_state):
    with TestClient(app) as client:
        r1 = client.post("/matrix/row", json={
            "framework": "langchain", "api": "chat",
            "stream": False, "state": False,
            "llm": "ollama", "mcp": "NONE", "routing": "direct",
        })
        src_id = r1.json()["row_id"]

        r2 = client.post(f"/matrix/row/{src_id}/clone-baseline")
        assert r2.status_code == 400
        assert "already" in r2.json().get("detail", "").lower()
