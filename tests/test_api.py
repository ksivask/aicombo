"""Tests for harness/api.py — FastAPI endpoints via TestClient."""
from fastapi.testclient import TestClient

from main import app


def test_health_returns_ok():
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_providers_returns_all_five():
    with TestClient(app) as client:
        r = client.get("/providers")
        assert r.status_code == 200
        ids = {p["id"] for p in r.json()["providers"]}
        assert {"NONE", "ollama", "claude", "chatgpt", "gemini"} == ids


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
