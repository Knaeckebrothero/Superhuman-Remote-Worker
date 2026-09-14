"""Unit tests for the Codex subscription usage normalization helpers.

The live fetch path (management-API token download → ChatGPT ``wham/usage``) is an
integration verified against the running proxy; these tests pin the pure
normalization logic the ``/api/codex/usage`` endpoint applies to the response —
window shaping and the ChatGPT-account-id extraction from the id_token JWT.

The helpers moved from ``orchestrator.main`` into
``orchestrator.services.subscriptions`` with the AI Subscriptions work; the
contract they pin is unchanged.
"""

from __future__ import annotations

import base64
import json

import pytest

from orchestrator.services.subscriptions import (
    SubscriptionProxyError,
    account_from_entry,
    decode_account_id,
    encode_account_id,
    extract_callback_params,
    fetch_codex_usage,
    sanitize_upstream_detail,
)
from orchestrator.services.subscriptions import (
    chatgpt_account_id as _chatgpt_account_id,
)
from orchestrator.services.subscriptions import (
    decode_jwt_claims as _decode_jwt_claims,
)
from orchestrator.services.subscriptions import (
    usage_window as _codex_usage_window,
)


def _jwt(payload: dict) -> str:
    """Build an unsigned JWT with ``payload`` (we only decode, never verify)."""

    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'none'})}.{b64(payload)}.sig"


class TestCodexUsageWindow:
    def test_normalizes_a_window(self):
        w = {
            "used_percent": 42,
            "limit_window_seconds": 18000,
            "reset_after_seconds": 12000,
            "reset_at": 1782935038,
            "extra": "ignored",
        }
        assert _codex_usage_window(w) == {
            "used_percent": 42,
            "window_seconds": 18000,
            "reset_after_seconds": 12000,
            "reset_at": 1782935038,
        }

    def test_none_for_non_dict(self):
        assert _codex_usage_window(None) is None
        assert _codex_usage_window("nope") is None

    def test_missing_fields_become_none(self):
        assert _codex_usage_window({}) == {
            "used_percent": None,
            "window_seconds": None,
            "reset_after_seconds": None,
            "reset_at": None,
        }


class TestChatgptAccountId:
    def test_extracts_from_openai_auth_claim(self):
        tok = _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acc-123"}})
        assert _chatgpt_account_id({"id_token": tok}) == "acc-123"

    def test_falls_back_to_top_level_claim(self):
        tok = _jwt({"chatgpt_account_id": "acc-top"})
        assert _chatgpt_account_id({"id_token": tok}) == "acc-top"

    def test_none_when_absent_or_garbage(self):
        assert _chatgpt_account_id({}) is None
        assert _chatgpt_account_id({"id_token": "not-a-jwt"}) is None
        assert _chatgpt_account_id({"id_token": ""}) is None


class TestDecodeJwtClaims:
    def test_decodes_payload(self):
        assert _decode_jwt_claims(_jwt({"a": 1, "b": "x"})) == {"a": 1, "b": "x"}

    def test_empty_on_garbage(self):
        assert _decode_jwt_claims("not-a-jwt") == {}
        assert _decode_jwt_claims("") == {}


class TestAccountIdentity:
    """The proxy's ``auth_index`` is a runtime position, not an identity."""

    def test_account_id_round_trips_a_credential_name(self):
        name = "codex-user@example.com-plus-ab12cd34.json"
        assert decode_account_id(encode_account_id(name)) == name

    def test_account_id_is_path_safe(self):
        encoded = encode_account_id("codex-user@example.com/../etc.json")
        assert "/" not in encoded and "@" not in encoded

    def test_garbage_account_id_is_a_404_not_a_crash(self):
        with pytest.raises(SubscriptionProxyError) as excinfo:
            decode_account_id("!!!not-base64!!!")
        assert excinfo.value.status == 404


class TestAccountProjection:
    """Only whitelisted fields reach the browser."""

    def _entry(self, **overrides):
        entry = {
            "id": "codex-a.json",
            "name": "codex-a.json",
            "provider": "codex",
            "status": "active",
            "email": "a@example.com",
            "auth_index": "0",
            "path": "/data/auth/codex-a.json",
            "id_token": {"sub": "secret-subject"},
        }
        entry.update(overrides)
        return entry

    def test_drops_path_and_token_claims(self):
        account = account_from_entry(self._entry())
        assert account is not None
        public = account.to_public()
        assert "path" not in public
        assert "id_token" not in public
        assert public["scope"] == "installation"

    def test_maps_channel_to_provider(self):
        account = account_from_entry(self._entry())
        assert account is not None
        assert account.channel == "codex"
        assert account.provider_key == "openai-codex"

    def test_anthropic_channel_alias_normalizes_to_claude(self):
        account = account_from_entry(self._entry(provider="anthropic"))
        assert account is not None
        assert account.channel == "claude"
        assert account.provider_key == "anthropic-claude-code"

    def test_cooldown_is_distinct_from_hard_error(self):
        cooled = account_from_entry(
            self._entry(unavailable=True, next_retry_after="2026-09-07T12:00:00Z")
        )
        broken = account_from_entry(self._entry(unavailable=True))
        assert cooled is not None and broken is not None
        assert cooled.state == "cooldown"
        assert broken.state == "error"

    def test_disabled_wins_over_status(self):
        account = account_from_entry(self._entry(disabled=True))
        assert account is not None
        assert account.state == "disabled"
        assert not account.healthy


class TestUsageScoping:
    """No non-Codex credential may ever reach the ChatGPT usage endpoint."""

    @pytest.mark.asyncio
    async def test_refuses_a_non_codex_account(self):
        account = account_from_entry(
            {"name": "claude-a.json", "provider": "claude", "status": "active"}
        )
        assert account is not None
        with pytest.raises(SubscriptionProxyError):
            await fetch_codex_usage(account)


class TestCallbackParsing:
    def test_extracts_code_and_state(self):
        code, state, error = extract_callback_params(
            "http://localhost:1455/auth/callback?code=abc123&state=st-1"
        )
        assert (code, state, error) == ("abc123", "st-1", None)

    def test_extracts_provider_denial(self):
        _, _, error = extract_callback_params(
            "http://localhost:1455/auth/callback?error=access_denied&state=st-1"
        )
        assert error == "access_denied"

    def test_bare_query_string_is_accepted(self):
        code, state, _ = extract_callback_params("?code=abc&state=st")
        assert (code, state) == ("abc", "st")

    def test_empty_input_yields_nothing(self):
        assert extract_callback_params("") == (None, None, None)


class TestUpstreamDetailSanitizer:
    def test_redacts_token_shaped_values(self):
        cleaned = sanitize_upstream_detail(
            '{"error":"bad","access_token":"sk-abcdef123456789"}'
        )
        assert "sk-abcdef123456789" not in cleaned
        assert "<redacted>" in cleaned

    def test_truncates(self):
        assert len(sanitize_upstream_detail("x" * 500)) == 200
