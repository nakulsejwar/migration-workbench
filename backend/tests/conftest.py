"""Shared fixtures for Migration Workbench tests.

Run from the backend directory with: python -m pytest -q
Dependencies are declared in requirements.txt; tests intentionally use the real
transformation engine and a temporary SQLite database, never the developer DB.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings


BACKEND_DIR = Path(__file__).resolve().parents[1]
SAMPLE_PATH = BACKEND_DIR / "app" / "data" / "sample_source.json"


@pytest.fixture(scope="session")
def sample_records() -> list[dict]:
    return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def app(tmp_path, monkeypatch):
    """Create an isolated application/database for each integration test.

    main.py creates a module-level ASGI app on import; point that bootstrap app
    at a temp-directory database so importing it never touches a project DB.
    """
    bootstrap_db = Path(tempfile.gettempdir()) / "migration-workbench-test-bootstrap.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{bootstrap_db.as_posix()}")
    from app.main import create_app

    db_path = tmp_path / "workbench-test.db"
    settings = Settings(
        database_url=f"sqlite:///{db_path.as_posix()}",
        openrouter_api_key="",
        demo_users=("alice", "bob"),
        cors_origins=(),
        enable_test_runner=False,
    )
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def demo_headers():
    return {"X-Demo-User": "alice"}
