"""Unit tests for the live citation-verdict callback (engine ``on_verdict``).

Covers the seam wired by the live verdict-push slice: ``CitationEngine`` fires
its optional ``on_verdict(citation_id, status)`` listener after a background
verification lands a *real* verdict, and stays silent on an aux outage. The
persistent session points this listener at a WS/SSE broadcast (cockpit
``citation.verdict`` patches the citation in place); worker jobs leave it
``None`` so the engine no-ops.

No Postgres / aux LLM needed — the db is a mock and the aux funnel
(``verify_and_store_citation``) is monkeypatched, since ``_run_verification``
imports it at call time.
"""

import types
from unittest.mock import MagicMock

import pytest

from src.citation_engine.engine import CitationEngine


def _engine(on_verdict=None):
    # db only needs to be non-None — _run_verification's I/O is patched out.
    return CitationEngine(db=MagicMock(), verify_aux=MagicMock(), on_verdict=on_verdict)


def _patch_verify(monkeypatch, return_value):
    """Stub the aux funnel that ``_run_verification`` imports at call time."""
    import src.services.auxiliary as aux

    async def _fake(*_args, **_kwargs):
        return return_value

    monkeypatch.setattr(aux, "verify_and_store_citation", _fake)


@pytest.mark.asyncio
async def test_on_verdict_fires_verified(monkeypatch):
    _patch_verify(monkeypatch, types.SimpleNamespace(verified=True))
    seen = []
    eng = _engine(on_verdict=lambda cid, status: seen.append((cid, status)))
    await eng._run_verification(42)
    assert seen == [(42, "verified")]


@pytest.mark.asyncio
async def test_on_verdict_fires_failed(monkeypatch):
    _patch_verify(monkeypatch, types.SimpleNamespace(verified=False))
    seen = []
    eng = _engine(on_verdict=lambda cid, status: seen.append((cid, status)))
    await eng._run_verification(7)
    assert seen == [(7, "failed")]


@pytest.mark.asyncio
async def test_no_fire_on_aux_outage(monkeypatch):
    # Aux outage → verify_and_store_citation returns None → the row stays
    # 'pending', so there is no state change to push.
    _patch_verify(monkeypatch, None)
    seen = []
    eng = _engine(on_verdict=lambda cid, status: seen.append((cid, status)))
    await eng._run_verification(7)
    assert seen == []


@pytest.mark.asyncio
async def test_no_callback_is_safe(monkeypatch):
    # Worker path: on_verdict unset. Must not raise.
    _patch_verify(monkeypatch, types.SimpleNamespace(verified=True))
    eng = _engine(on_verdict=None)
    await eng._run_verification(1)  # no exception == pass


@pytest.mark.asyncio
async def test_callback_exception_is_swallowed(monkeypatch):
    # A throwing listener must not break the fire-and-forget verifier.
    _patch_verify(monkeypatch, types.SimpleNamespace(verified=True))

    def _boom(_cid, _status):
        raise RuntimeError("listener blew up")

    eng = _engine(on_verdict=_boom)
    await eng._run_verification(1)  # no exception == pass
