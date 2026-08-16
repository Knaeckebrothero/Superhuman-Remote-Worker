"""The dev preview route renders every email through its real builder."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from orchestrator.main import app

    return TestClient(app)


@pytest.mark.parametrize("name", ["system", "agent", "permission"])
def test_preview_renders_each_email(client, name) -> None:
    resp = client.get(f"/debug/emails/{name}")
    assert resp.status_code == 200
    assert "<!DOCTYPE html>" in resp.text
    assert "#1e1e2e" not in resp.text


def test_preview_index_lists_all(client) -> None:
    resp = client.get("/debug/emails")
    assert resp.status_code == 200
    for name in ("system", "agent", "permission"):
        assert name in resp.text


def test_unknown_preview_is_404(client) -> None:
    assert client.get("/debug/emails/nope").status_code == 404
