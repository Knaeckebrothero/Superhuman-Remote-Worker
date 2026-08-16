"""The dev preview route renders every email through its real builder.

Gated by EMAIL_PREVIEW_ENABLED (default off, same convention as
CANVAS_LIVE_PREVIEW_ENABLED / COLLABORA_ENABLED): tests that exercise the
rendered output opt the flag on via monkeypatch. A separate pair confirms the
gate itself is both present and non-leaky — disabled, the response body must
match a genuinely unmapped route's, not just its status code.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from orchestrator.main import app

    return TestClient(app)


@pytest.fixture
def preview_enabled(monkeypatch):
    monkeypatch.setenv("EMAIL_PREVIEW_ENABLED", "true")


@pytest.mark.parametrize("name", ["system", "agent", "permission"])
def test_preview_renders_each_email(client, preview_enabled, name) -> None:
    resp = client.get(f"/debug/emails/{name}")
    assert resp.status_code == 200
    assert "<!DOCTYPE html>" in resp.text
    assert "#1e1e2e" not in resp.text


def test_preview_index_lists_all(client, preview_enabled) -> None:
    resp = client.get("/debug/emails")
    assert resp.status_code == 200
    for name in ("system", "agent", "permission"):
        assert name in resp.text


def test_unknown_preview_is_404(client, preview_enabled) -> None:
    assert client.get("/debug/emails/nope").status_code == 404


def test_preview_index_is_404_when_flag_unset(client, monkeypatch) -> None:
    """Disabled must be indistinguishable from never-registered: same body.

    Not just the same status code — a route-specific detail string (even an
    innocuous one) is an oracle that tells a prober the feature exists but is
    switched off. Compare against a genuinely unmapped path's own 404 rather
    than hardcoding "Not Found", so this stays correct if FastAPI's default
    ever changes.
    """
    monkeypatch.delenv("EMAIL_PREVIEW_ENABLED", raising=False)
    baseline = client.get("/definitely-not-a-route-xyz")
    resp = client.get("/debug/emails")
    assert resp.status_code == baseline.status_code == 404
    assert resp.json() == baseline.json()


def test_preview_route_is_404_when_flag_unset(client, monkeypatch) -> None:
    """Same as above, for the /{name} route — see that test's docstring."""
    monkeypatch.delenv("EMAIL_PREVIEW_ENABLED", raising=False)
    baseline = client.get("/definitely-not-a-route-xyz")
    resp = client.get("/debug/emails/system")
    assert resp.status_code == baseline.status_code == 404
    assert resp.json() == baseline.json()
