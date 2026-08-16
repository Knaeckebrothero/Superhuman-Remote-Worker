"""The dev preview route renders every email through its real builder.

Gated by EMAIL_PREVIEW_ENABLED (default off, same convention as
CANVAS_LIVE_PREVIEW_ENABLED / COLLABORA_ENABLED): tests that exercise the
rendered output opt the flag on via monkeypatch; a separate pair confirms
the gate itself, so removing it would fail those two.
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
    monkeypatch.delenv("EMAIL_PREVIEW_ENABLED", raising=False)
    assert client.get("/debug/emails").status_code == 404


def test_preview_route_is_404_when_flag_unset(client, monkeypatch) -> None:
    monkeypatch.delenv("EMAIL_PREVIEW_ENABLED", raising=False)
    assert client.get("/debug/emails/system").status_code == 404
