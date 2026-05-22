"""Tests for POST /api/sessions/{tid}/prepare."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def app(monkeypatch):
    """Minimal FastAPI app with the sessions router mounted."""
    from orchestrator.routers.sessions import router as sessions_router

    app = FastAPI()

    # Override require_approved_user dependency to return a fixed user.
    from security.auth import require_approved_user
    app.dependency_overrides[require_approved_user] = lambda: {
        "id": "u1", "is_approved": True
    }

    fake_db = AsyncMock()
    fake_db.get_thread = AsyncMock(return_value={
        "id": "t1",
        "user_id": "u1",
        "agent_id": None,
        "config_name": "persistent_defaults",
    })
    # advisory lock context manager
    lock_cm = AsyncMock()
    lock_cm.__aenter__.return_value = None
    lock_cm.__aexit__.return_value = False
    fake_db.thread_advisory_lock = MagicMock(return_value=lock_cm)

    # The router uses `from main import postgres_db` — patch the symbol on the
    # router module so the late import resolves to our fake.
    monkeypatch.setattr(
        "orchestrator.routers.sessions._get_db",
        lambda: fake_db,
        raising=False,
    )

    app.include_router(sessions_router)
    return app, fake_db


def test_prepare_returns_202_immediately(app):
    """POST /prepare returns 202 with state=provisioning before any work."""
    fastapi_app, fake_db = app
    client = TestClient(fastapi_app)

    resp = client.post("/api/sessions/t1/prepare", json={})
    assert resp.status_code == 202
    assert resp.json() == {"state": "provisioning"}


def test_prepare_returns_404_for_missing_thread(app):
    """POST /prepare returns 404 if thread doesn't exist."""
    fastapi_app, fake_db = app
    fake_db.get_thread.return_value = None
    client = TestClient(fastapi_app)

    resp = client.post("/api/sessions/missing/prepare", json={})
    assert resp.status_code == 404


def test_prepare_returns_403_when_thread_owned_by_other_user(app):
    """POST /prepare returns 403 if caller is not the thread owner."""
    fastapi_app, fake_db = app
    fake_db.get_thread.return_value = {
        "id": "t1", "user_id": "OTHER", "agent_id": None
    }
    client = TestClient(fastapi_app)

    resp = client.post("/api/sessions/t1/prepare", json={})
    assert resp.status_code == 403
