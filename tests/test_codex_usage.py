"""Unit tests for the Codex subscription usage normalization helpers.

The live fetch path (management-API token download → ChatGPT ``wham/usage``) is an
integration verified against the running proxy; these tests pin the pure
normalization logic the ``/api/codex/usage`` endpoint applies to the response —
window shaping and the ChatGPT-account-id extraction from the id_token JWT.
"""

from __future__ import annotations

import base64
import json

from orchestrator.main import (
    _chatgpt_account_id,
    _codex_usage_window,
    _decode_jwt_claims,
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
