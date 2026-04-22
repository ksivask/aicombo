"""Shared pytest fixtures for aiplay harness tests."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Make harness/ importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "harness"))


@pytest.fixture
def tmp_data_dir(monkeypatch):
    """Isolate trial JSON writes in a temp directory per test."""
    with tempfile.TemporaryDirectory() as d:
        data_dir = Path(d) / "trials"
        data_dir.mkdir()
        monkeypatch.setenv("DATA_DIR", str(Path(d)))
        yield Path(d)
