"""Shared pytest fixtures for aiplay harness tests."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Make harness/ importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "harness"))

# Ensure DATA_DIR points at a writable location before any `import api`
# happens during test collection (api.py creates TrialStore at module
# import time). Individual tests can still override via `reset_api_state`.
if not os.environ.get("DATA_DIR"):
    _default_tmp = Path(tempfile.gettempdir()) / "aiplay-test-data"
    _default_tmp.mkdir(parents=True, exist_ok=True)
    os.environ["DATA_DIR"] = str(_default_tmp)

# Prevent harness/main.py's lifespan from spawning docker ps / docker logs -f
# subprocesses when tests exercise it via `with TestClient(app) as client:`.
# The gate is consumed in main.lifespan; api.AUDIT_TAIL stays None so the
# rest of api.py short-circuits cleanly.
os.environ["AIPLAY_DISABLE_AUDIT_TAIL"] = "1"


@pytest.fixture
def tmp_data_dir(monkeypatch):
    """Isolate trial JSON writes in a temp directory per test."""
    with tempfile.TemporaryDirectory() as d:
        data_dir = Path(d) / "trials"
        data_dir.mkdir()
        monkeypatch.setenv("DATA_DIR", str(Path(d)))
        yield Path(d)


@pytest.fixture(autouse=False)
def reset_api_state(tmp_data_dir, monkeypatch):
    """Rewire harness/api.py module-level state to point at tmp_data_dir.
    Tests that exercise API endpoints should use this fixture explicitly."""
    import api
    from trials import TrialStore
    monkeypatch.setattr(api, "DATA_DIR", tmp_data_dir)
    monkeypatch.setattr(api, "STORE", TrialStore(tmp_data_dir / "trials"))
    monkeypatch.setattr(api, "MATRIX_PATH", tmp_data_dir / "matrix.json")
    yield
