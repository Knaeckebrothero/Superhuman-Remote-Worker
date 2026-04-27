"""Tests for ``orchestrator.seed.llm_config.ensure_codex_proxy_endpoint``.

Runtime helper that wires the codex-proxy as a system endpoint when a user
connects a ChatGPT subscription via the cockpit, or when the Admin → Models
availability probe sees an active subscription with no transport row. Unlike
the boot-time seeder (``orchestrator.init._seed_codex_proxy_endpoint``), this
helper does not require ``CODEX_PROXY_URL`` to be set — it falls back to the
same default the runtime resolver uses.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.seed.llm_config import (
    CODEX_PROXY_ENDPOINT_LABEL,
    ensure_codex_proxy_endpoint,
)


def _fake_db(*, existing_endpoints: list[dict] | None = None):
    db = MagicMock()
    db.list_system_api_keys = AsyncMock(return_value=[])
    db.list_system_llm_endpoints = AsyncMock(
        return_value=list(existing_endpoints or [])
    )
    db.create_system_llm_endpoint = AsyncMock(
        return_value={"id": "00000000-0000-0000-0000-000000000099"}
    )
    return db


@pytest.mark.asyncio
async def test_creates_row_when_absent(monkeypatch):
    """No prior row + active subscription → helper inserts a codex-proxy row."""
    monkeypatch.setenv("CODEX_PROXY_URL", "http://codex-proxy:8317")
    monkeypatch.setenv("CODEX_MANAGEMENT_KEY", "sk-mgmt-test")

    db = _fake_db()
    created = await ensure_codex_proxy_endpoint(db)

    assert created is True
    db.create_system_llm_endpoint.assert_awaited_once()
    kwargs = db.create_system_llm_endpoint.await_args.kwargs
    assert kwargs["label"] == CODEX_PROXY_ENDPOINT_LABEL == "codex-proxy"
    assert kwargs["base_url"] == "http://codex-proxy:8317/v1"
    assert kwargs["api_key"] == "sk-mgmt-test"


@pytest.mark.asyncio
async def test_no_op_when_row_already_exists(monkeypatch):
    """Existing row → helper returns False, no insert. Repeat callback safe."""
    monkeypatch.setenv("CODEX_PROXY_URL", "http://codex-proxy:8317")
    monkeypatch.setenv("CODEX_MANAGEMENT_KEY", "sk-mgmt-test")

    db = _fake_db(
        existing_endpoints=[
            {
                "id": "00000000-0000-0000-0000-000000000077",
                "label": "codex-proxy",
                "base_url": "http://codex-proxy:8317/v1",
            }
        ]
    )
    created = await ensure_codex_proxy_endpoint(db)

    assert created is False
    db.create_system_llm_endpoint.assert_not_awaited()


@pytest.mark.asyncio
async def test_falls_back_to_default_proxy_url_when_env_unset(monkeypatch):
    """The reproducer for the original bug: env var unset, the user's local
    stack still works because Settings → Codex hits the proxy via the runtime
    fallback. The wire-up helper must use the same fallback so connecting via
    the UI auto-creates the transport row even without ``CODEX_PROXY_URL``."""
    monkeypatch.delenv("CODEX_PROXY_URL", raising=False)
    monkeypatch.setenv("CODEX_MANAGEMENT_KEY", "sk-mgmt-test")

    db = _fake_db()
    created = await ensure_codex_proxy_endpoint(db)

    assert created is True
    kwargs = db.create_system_llm_endpoint.await_args.kwargs
    assert kwargs["base_url"] == "http://localhost:8317/v1"


@pytest.mark.asyncio
async def test_explicit_proxy_url_overrides_env(monkeypatch):
    """The availability probe passes the resolved proxy_url through. The
    helper honours the explicit value over the env var so the response and
    the transport row stay in lockstep."""
    monkeypatch.setenv("CODEX_PROXY_URL", "http://env-host:8317")
    monkeypatch.setenv("CODEX_MANAGEMENT_KEY", "sk-mgmt-test")

    db = _fake_db()
    await ensure_codex_proxy_endpoint(db, proxy_url="https://explicit.example.com")

    kwargs = db.create_system_llm_endpoint.await_args.kwargs
    assert kwargs["base_url"] == "https://explicit.example.com/v1"


@pytest.mark.asyncio
async def test_appends_v1_when_missing(monkeypatch):
    """Bare URLs get ``/v1`` appended — the proxy enforces that prefix."""
    monkeypatch.setenv("CODEX_MANAGEMENT_KEY", "sk-mgmt-test")
    db = _fake_db()
    await ensure_codex_proxy_endpoint(db, proxy_url="https://proxy.example.com")
    kwargs = db.create_system_llm_endpoint.await_args.kwargs
    assert kwargs["base_url"] == "https://proxy.example.com/v1"


@pytest.mark.asyncio
async def test_preserves_v1_when_present(monkeypatch):
    """A URL that already ends in ``/v1`` is not double-suffixed."""
    monkeypatch.setenv("CODEX_MANAGEMENT_KEY", "sk-mgmt-test")
    db = _fake_db()
    await ensure_codex_proxy_endpoint(db, proxy_url="http://codex-proxy:8317/v1")
    kwargs = db.create_system_llm_endpoint.await_args.kwargs
    assert kwargs["base_url"] == "http://codex-proxy:8317/v1"


@pytest.mark.asyncio
async def test_seed_failure_returns_false(monkeypatch):
    """A DB failure during the seed must be swallowed — wiring is best-effort
    and a hiccup on a transport row should never break the OAuth callback."""
    monkeypatch.setenv("CODEX_PROXY_URL", "http://codex-proxy:8317")
    monkeypatch.setenv("CODEX_MANAGEMENT_KEY", "sk-mgmt-test")

    db = _fake_db()
    db.list_system_llm_endpoints.side_effect = RuntimeError("boom")

    created = await ensure_codex_proxy_endpoint(db)
    assert created is False
