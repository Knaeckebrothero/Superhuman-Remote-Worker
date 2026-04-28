"""Tests for ``orchestrator.init._seed_codex_proxy_endpoint``.

Seeds a system-scoped llm_endpoints row (label=``codex-proxy``) when the
``CODEX_PROXY_URL`` env var is set. Re-uses the existing helm-style seeder
under the hood so re-runs are no-ops by label match.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator import init as init_mod


def _fake_db(*, existing_endpoints: list[dict] | None = None):
    db = MagicMock()
    # _seed_api_keys runs first inside seed(); even with an empty
    # systemApiKeys list it inspects existing rows, so we mock both branches.
    db.list_system_api_keys = AsyncMock(return_value=[])
    db.list_system_llm_endpoints = AsyncMock(
        return_value=list(existing_endpoints or [])
    )
    db.create_system_llm_endpoint = AsyncMock(
        return_value={"id": "00000000-0000-0000-0000-000000000099"}
    )
    return db


@pytest.mark.asyncio
async def test_no_codex_proxy_url_is_noop(monkeypatch):
    """Without CODEX_PROXY_URL, the seed bails before any DB calls."""
    monkeypatch.delenv("CODEX_PROXY_URL", raising=False)
    monkeypatch.delenv("CODEX_MANAGEMENT_KEY", raising=False)

    db = _fake_db()
    await init_mod._seed_codex_proxy_endpoint(db)

    db.list_system_llm_endpoints.assert_not_awaited()
    db.create_system_llm_endpoint.assert_not_awaited()


@pytest.mark.asyncio
async def test_seeds_codex_proxy_endpoint(monkeypatch):
    """With CODEX_PROXY_URL set, seeder inserts a row labelled ``codex-proxy``."""
    monkeypatch.setenv("CODEX_PROXY_URL", "http://codex-proxy:8317")
    monkeypatch.setenv("CODEX_MANAGEMENT_KEY", "sk-mgmt-test")

    db = _fake_db()
    await init_mod._seed_codex_proxy_endpoint(db)

    db.create_system_llm_endpoint.assert_awaited_once()
    kwargs = db.create_system_llm_endpoint.await_args.kwargs
    assert kwargs["label"] == init_mod.CODEX_PROXY_ENDPOINT_LABEL == "codex-proxy"
    assert kwargs["base_url"] == "http://codex-proxy:8317/v1"
    # Management key was resolved via apiKeyEnv and inlined for the insert.
    assert kwargs["api_key"] == "sk-mgmt-test"


@pytest.mark.asyncio
async def test_appends_v1_when_url_lacks_it(monkeypatch):
    """A bare proxy URL (no /v1) gets the suffix appended automatically."""
    monkeypatch.setenv("CODEX_PROXY_URL", "https://proxy.example.com")
    monkeypatch.setenv("CODEX_MANAGEMENT_KEY", "sk-mgmt-test")

    db = _fake_db()
    await init_mod._seed_codex_proxy_endpoint(db)

    kwargs = db.create_system_llm_endpoint.await_args.kwargs
    assert kwargs["base_url"] == "https://proxy.example.com/v1"


@pytest.mark.asyncio
async def test_preserves_v1_when_already_present(monkeypatch):
    """A URL that already ends in /v1 is left untouched."""
    monkeypatch.setenv("CODEX_PROXY_URL", "http://codex-proxy:8317/v1")
    monkeypatch.setenv("CODEX_MANAGEMENT_KEY", "sk-mgmt-test")

    db = _fake_db()
    await init_mod._seed_codex_proxy_endpoint(db)

    kwargs = db.create_system_llm_endpoint.await_args.kwargs
    assert kwargs["base_url"] == "http://codex-proxy:8317/v1"


@pytest.mark.asyncio
async def test_existing_row_skipped(monkeypatch):
    """An existing codex-proxy row is not overwritten — admin rotations win."""
    monkeypatch.setenv("CODEX_PROXY_URL", "http://codex-proxy:8317")
    monkeypatch.setenv("CODEX_MANAGEMENT_KEY", "sk-mgmt-test")

    db = _fake_db(
        existing_endpoints=[
            {
                "id": "00000000-0000-0000-0000-000000000077",
                "label": "codex-proxy",
                "base_url": "http://old-host:8317/v1",
            }
        ]
    )
    await init_mod._seed_codex_proxy_endpoint(db)

    db.create_system_llm_endpoint.assert_not_awaited()


@pytest.mark.asyncio
async def test_seeds_without_management_key(monkeypatch):
    """Missing CODEX_MANAGEMENT_KEY downgrades to a keyless row (the seeder
    logs a warning but does not fail). The row still gets created so admins
    can fix the key later via Admin → Providers."""
    monkeypatch.setenv("CODEX_PROXY_URL", "http://codex-proxy:8317")
    monkeypatch.delenv("CODEX_MANAGEMENT_KEY", raising=False)

    db = _fake_db()
    await init_mod._seed_codex_proxy_endpoint(db)

    db.create_system_llm_endpoint.assert_awaited_once()
    kwargs = db.create_system_llm_endpoint.await_args.kwargs
    assert kwargs["label"] == "codex-proxy"
    assert kwargs["api_key"] is None
