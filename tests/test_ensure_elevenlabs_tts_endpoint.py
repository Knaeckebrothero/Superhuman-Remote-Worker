"""Tests for ``orchestrator.seed.llm_config.ensure_elevenlabs_tts_endpoint``.

Boot-time helper that auto-registers the ElevenLabs read-aloud provider when
``ELEVENLABS_API_KEY`` is present — key in the secret is all it takes, no manual
Admin step (mirrors the codex-proxy wiring). The endpoint row stores no key (env
is the source of truth; the TTS adapter reads it), and the catalog row carries
``params_json.provider`` + a default voice so playback works out of the box.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.seed.llm_config import (
    ELEVENLABS_DEFAULT_VOICE,
    ELEVENLABS_ENDPOINT_LABEL,
    ELEVENLABS_TTS_MODEL_ID,
    ensure_elevenlabs_tts_endpoint,
)


def _fake_db(*, existing_endpoints: list[dict] | None = None, model_inserted=True):
    db = MagicMock()
    db.list_system_llm_endpoints = AsyncMock(
        return_value=list(existing_endpoints or [])
    )
    db.create_system_llm_endpoint = AsyncMock(
        return_value={"id": "00000000-0000-0000-0000-0000000000ee"}
    )
    db.create_model = AsyncMock(
        return_value={"model_id": ELEVENLABS_TTS_MODEL_ID} if model_inserted else None
    )
    return db


@pytest.mark.asyncio
async def test_creates_rows_when_key_set(monkeypatch):
    """Key present, nothing pre-existing → endpoint + tts model are created."""
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_test_key")
    db = _fake_db()

    created = await ensure_elevenlabs_tts_endpoint(db)

    assert created is True
    db.create_system_llm_endpoint.assert_awaited_once()
    ep_kwargs = db.create_system_llm_endpoint.await_args.kwargs
    assert ep_kwargs["label"] == ELEVENLABS_ENDPOINT_LABEL
    # Env is the source of truth: the endpoint stores NO key.
    assert ep_kwargs["api_key"] is None

    db.create_model.assert_awaited_once()
    m = db.create_model.await_args.kwargs
    assert m["provider_kind"] == "endpoint"
    assert m["model_id"] == ELEVENLABS_TTS_MODEL_ID
    assert m["capabilities"] == ["tts"]
    # Routes to the ElevenLabs adapter + ships a working default voice.
    assert m["params_json"]["provider"] == "elevenlabs"
    assert m["params_json"]["voice"] == ELEVENLABS_DEFAULT_VOICE
    assert m["on_conflict_do_nothing"] is True


@pytest.mark.asyncio
async def test_no_op_when_key_absent(monkeypatch):
    """No key → helper does nothing (no dangling rows on a keyless deployment)."""
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    db = _fake_db()

    created = await ensure_elevenlabs_tts_endpoint(db)

    assert created is False
    db.create_system_llm_endpoint.assert_not_awaited()
    db.create_model.assert_not_awaited()


@pytest.mark.asyncio
async def test_reuses_existing_endpoint(monkeypatch):
    """A prior ElevenLabs endpoint is reused — no duplicate; model anchors to it."""
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_test_key")
    db = _fake_db(
        existing_endpoints=[
            {"id": "11111111-1111-1111-1111-111111111111", "label": "ElevenLabs"}
        ]
    )

    await ensure_elevenlabs_tts_endpoint(db)

    db.create_system_llm_endpoint.assert_not_awaited()
    assert (
        db.create_model.await_args.kwargs["provider_ref"]
        == "11111111-1111-1111-1111-111111111111"
    )


@pytest.mark.asyncio
async def test_idempotent_model_conflict_returns_false(monkeypatch):
    """Model already present (ON CONFLICT DO NOTHING → None) → returns False."""
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_test_key")
    db = _fake_db(model_inserted=False)

    created = await ensure_elevenlabs_tts_endpoint(db)
    assert created is False


@pytest.mark.asyncio
async def test_failure_is_swallowed(monkeypatch):
    """A DB hiccup must never abort startup — helper logs and returns False."""
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_test_key")
    db = _fake_db()
    db.list_system_llm_endpoints.side_effect = RuntimeError("boom")

    created = await ensure_elevenlabs_tts_endpoint(db)
    assert created is False
