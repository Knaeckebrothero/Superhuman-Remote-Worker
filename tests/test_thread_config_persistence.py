"""Tests for the persistent-session credential at-rest fix.

Two guarantees:
1. ``_inject_thread_dispatch_credentials`` is idempotent + re-injection-safe —
   running it on a *stripped* config_override (the at-rest representation)
   repopulates exactly the removed secrets. This is what lets session
   attach/resume work after secrets stop being persisted.
2. ``redact_config_override`` round-trips with the injector (strip → re-inject
   restores the keys), and ``backfill_strip_thread_config_secrets`` removes
   legacy plaintext idempotently.

Harness mirrors tests/test_dispatch_phase_credentials.py.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

_ORCH = Path(__file__).parent.parent / "orchestrator"
if str(_ORCH) not in sys.path:
    sys.path.insert(0, str(_ORCH))

os.environ.setdefault("VECTOR_DB_URL", "postgresql://test@localhost/test")

import main  # noqa: E402
from security.access import redact_config_override  # noqa: E402
from src.core.model_registry import ModelMeta  # noqa: E402


ENDPOINT_ID = "11111111-1111-1111-1111-111111111111"
BASE_URL = "https://ai.h4ll.app/v1"
API_KEY = "sk-endpoint-SECRET"


@pytest.fixture
def patched_main(monkeypatch):
    """Patch the DB + registry collaborators so the injector exercises only its
    branching. ``custom-model`` is an endpoint-backed model (inlines
    base_url+api_key); everything else is unknown."""

    async def fake_resolve(model_id, user_id=None, capability="chat"):
        if model_id == "custom-model":
            return ModelMeta(
                model_id="custom-model",
                provider="openai",
                family="gpt",
                display_name="Custom Model",
                origin="custom",
                endpoint_id=ENDPOINT_ID,
                api_key_ref="openai",
            )
        return None

    async def fake_get_endpoint(endpoint_id):
        if endpoint_id == ENDPOINT_ID:
            return {
                "id": ENDPOINT_ID,
                "label": "endpoint",
                "base_url": BASE_URL,
                "api_key": API_KEY,
            }
        return None

    monkeypatch.setattr(
        main, "_resolve_model", AsyncMock(side_effect=fake_resolve), raising=True
    )
    monkeypatch.setattr(
        main.postgres_db,
        "get_user_llm_endpoint",
        AsyncMock(side_effect=fake_get_endpoint),
    )
    monkeypatch.setattr(
        main.postgres_db, "resolve_api_keys_for_job", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        main.postgres_db,
        "resolve_default_for_capability",
        AsyncMock(return_value=None),
    )


class TestInjectThreadCredentials:
    @pytest.mark.asyncio
    async def test_fresh_injection(self, patched_main):
        co = {"llm": {"model": "custom-model"}}
        out = await main._inject_thread_dispatch_credentials(
            co, user_id="u", project_id="p"
        )
        assert out["llm"]["base_url"] == BASE_URL
        assert out["llm"]["api_key"] == API_KEY

    @pytest.mark.asyncio
    async def test_reinjection_after_strip(self, patched_main):
        """Keystone: a stripped copy (model + base_url survive, api_key removed)
        gets api_key re-injected. This is the resume/workspace-endpoint path."""
        stripped = {
            "llm": {"model": "custom-model", "provider": "openai", "base_url": BASE_URL}
        }
        out = await main._inject_thread_dispatch_credentials(
            stripped, user_id="u", project_id="p"
        )
        assert out["llm"]["api_key"] == API_KEY
        assert out["llm"]["base_url"] == BASE_URL

    @pytest.mark.asyncio
    async def test_none_transport_sentinels_are_repopulated(self, patched_main):
        """A prior hot-swap leaves provider/base_url=None sentinels in the stored
        copy; re-injection must treat them as absent and repopulate."""
        stored = {"llm": {"model": "custom-model", "provider": None, "base_url": None}}
        out = await main._inject_thread_dispatch_credentials(
            stored, user_id="u", project_id="p"
        )
        assert out["llm"]["base_url"] == BASE_URL
        assert out["llm"]["api_key"] == API_KEY

    @pytest.mark.asyncio
    async def test_redact_then_reinject_round_trip(self, patched_main):
        enriched = await main._inject_thread_dispatch_credentials(
            {"llm": {"model": "custom-model"}}, user_id="u", project_id="p"
        )
        stripped = redact_config_override(enriched)
        # The at-rest copy keeps model/base_url, drops the key.
        assert "api_key" not in stripped["llm"]
        assert stripped["llm"]["base_url"] == BASE_URL
        # Re-injection restores it.
        restored = await main._inject_thread_dispatch_credentials(
            stripped, user_id="u", project_id="p"
        )
        assert restored["llm"]["api_key"] == API_KEY


CODEX_ENDPOINT_ID = "44444444-4444-4444-4444-444444444444"
CODEX_BASE_URL = "http://srw-codex-proxy:8317/v1"
CODEX_API_KEY = "sk-codex-endpoint-test"
GATEWAY_BASE_URL = "http://srw-litellm:4000/v1"


@pytest.fixture
def patched_main_codex_gateway(monkeypatch):
    """A codex model (`gpt-5.5`) on the codex-proxy endpoint, with the LiteLLM
    gateway ENABLED — the session a1153f56 environment. project_id is omitted in
    the test so the scoped-key path (which would touch the gateway) is skipped
    and routing falls back to ``_gateway_routing_target`` (no network)."""

    async def fake_resolve(model_id, user_id=None, capability="chat"):
        if model_id == "gpt-5.5":
            return ModelMeta(
                model_id="gpt-5.5",
                provider="codex",
                family="gpt-5",
                display_name="GPT-5.5",
                origin="catalog",
                endpoint_id=CODEX_ENDPOINT_ID,
            )
        return None

    async def fake_get_endpoint(endpoint_id):
        if endpoint_id == CODEX_ENDPOINT_ID:
            return {
                "id": CODEX_ENDPOINT_ID,
                "label": "codex-proxy",
                "base_url": CODEX_BASE_URL,
                "api_key": CODEX_API_KEY,
            }
        return None

    monkeypatch.setattr(
        main, "_resolve_model", AsyncMock(side_effect=fake_resolve), raising=True
    )
    monkeypatch.setattr(
        main.postgres_db,
        "get_user_llm_endpoint",
        AsyncMock(side_effect=fake_get_endpoint),
    )
    monkeypatch.setattr(
        main.postgres_db, "resolve_api_keys_for_job", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        main.postgres_db,
        "resolve_default_for_capability",
        AsyncMock(return_value=None),
    )
    # Gateway enabled (chart sets these when litellm.enabled).
    monkeypatch.setenv("LITELLM_BASE_URL", "http://srw-litellm:4000")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-master-test")


class TestCodexSessionGatewayBaseUrl:
    """Regression for docs/issues/codex_session_gateway_baseurl_401.md."""

    @pytest.mark.asyncio
    async def test_stale_gateway_base_url_replaced_with_codex_endpoint(
        self, patched_main_codex_gateway
    ):
        """The session a1153f56 bug: a stored llm override pins a codex model
        with a STALE LiteLLM-gateway base_url (+ provider) carried over from a
        previously gateway-routed model. Re-injection on resume must route to
        the codex proxy with its own key — not ship the codex key to the gateway
        (which 401s "LiteLLM Virtual Key expected. Received=…, expected sk-")."""
        stored = {
            "llm": {
                "model": "gpt-5.5",
                "base_url": GATEWAY_BASE_URL,  # stale — the bug
                "provider": "openai",  # stale factory
            }
        }
        out = await main._inject_thread_dispatch_credentials(
            stored,
            user_id="u",  # no project_id → fleet/master fallback, no network
        )
        assert out["llm"]["base_url"] == CODEX_BASE_URL
        assert out["llm"]["api_key"] == CODEX_API_KEY
        assert out["llm"]["provider"] == "codex"
        # Belt and suspenders: the codex key must NOT be paired with the gateway.
        assert out["llm"]["base_url"] != GATEWAY_BASE_URL


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows
        self.updates: list = []

    async def fetch(self, *_a, **_k):
        return self._rows

    async def execute(self, _q, *args):
        self.updates.append(args)


class TestStripBackfill:
    @pytest.mark.asyncio
    async def test_strips_plaintext_and_skips_clean(self, monkeypatch):
        rows = [
            {
                "id": "t-dirty",
                "metadata": json.dumps(
                    {"config_override": {"llm": {"model": "m", "api_key": "SECRET"}}}
                ),
            },
            {
                "id": "t-clean",
                "metadata": json.dumps({"config_override": {"llm": {"model": "m"}}}),
            },
        ]
        conn = _FakeConn(rows)
        monkeypatch.setattr(main.postgres_db, "acquire", lambda: _FakeAcquire(conn))

        counts = await main.postgres_db.backfill_strip_thread_config_secrets()

        assert counts == {"stripped": 1, "skipped": 1, "errors": 0}
        # Exactly one UPDATE, and its payload carries no secret.
        assert len(conn.updates) == 1
        payload_json = conn.updates[0][0]
        assert "SECRET" not in payload_json
        assert json.loads(payload_json) == {"llm": {"model": "m"}}

    @pytest.mark.asyncio
    async def test_idempotent_second_pass_is_noop(self, monkeypatch):
        rows = [
            {
                "id": "t-clean",
                "metadata": json.dumps({"config_override": {"llm": {"model": "m"}}}),
            }
        ]
        conn = _FakeConn(rows)
        monkeypatch.setattr(main.postgres_db, "acquire", lambda: _FakeAcquire(conn))

        counts = await main.postgres_db.backfill_strip_thread_config_secrets()

        assert counts == {"stripped": 0, "skipped": 1, "errors": 0}
        assert conn.updates == []
