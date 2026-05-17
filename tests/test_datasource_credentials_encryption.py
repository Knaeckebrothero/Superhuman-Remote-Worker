"""Tests for the datasources.credentials JSONB encryption helpers.

The helpers under test live in ``orchestrator/database/postgres.py`` and bridge
the raw JSONB values that asyncpg surfaces (a JSON string here, since no JSON
codec is registered) with the orchestrator's typed credential dicts. They
delegate to :mod:`orchestrator.security.crypto` for the actual AES-GCM work,
which is covered by ``test_crypto.py``.
"""

from __future__ import annotations

import json
import logging

import pytest

from database.postgres import (
    _datasource_row_to_dict,
    _decrypt_credentials_field,
    _encrypt_credentials_dict,
)
from security import crypto


@pytest.fixture(autouse=True)
def _reset_cipher_cache():
    """The encryption key in conftest.py is already loaded; just make sure
    the cipher cache is warm and consistent across tests."""
    crypto.reset_cipher_cache()
    yield
    crypto.reset_cipher_cache()


# =============================================================================
# _encrypt_credentials_dict
# =============================================================================


class TestEncryptCredentialsDict:
    def test_empty_dict_is_not_encrypted(self):
        # No secret to protect; matches the schema default and keeps the
        # column compact for the common "no credentials" case.
        assert _encrypt_credentials_dict({}) == "{}"

    def test_none_is_not_encrypted(self):
        assert _encrypt_credentials_dict(None) == "{}"

    def test_non_empty_dict_is_encrypted(self):
        out = _encrypt_credentials_dict({"token": "secret"})
        # The returned value is a JSON-encoded string (the JSONB persistence
        # shape). Strip the outer quotes to see the v1 ciphertext.
        parsed = json.loads(out)
        assert isinstance(parsed, str)
        assert crypto.is_encrypted(parsed)

    def test_each_call_uses_fresh_nonce(self):
        # Two encryptions of the same dict must produce distinct ciphertexts
        # so the JSONB column doesn't leak equality.
        a = _encrypt_credentials_dict({"k": "v"})
        b = _encrypt_credentials_dict({"k": "v"})
        assert a != b


# =============================================================================
# _decrypt_credentials_field
# =============================================================================


class TestDecryptCredentialsField:
    def test_round_trip(self):
        original = {"env_vars": {"FOO": "bar"}, "ssh_key": "----PRIVATE----"}
        encrypted = _encrypt_credentials_dict(original)
        assert _decrypt_credentials_field(encrypted) == original

    def test_none_returns_empty_dict(self):
        assert _decrypt_credentials_field(None) == {}

    def test_empty_jsonb_returns_empty_dict(self):
        assert _decrypt_credentials_field("{}") == {}

    def test_legacy_plaintext_dict_returned_as_is(self, caplog):
        # asyncpg returns JSONB as a JSON string. Legacy rows stored the
        # credentials as a plain JSON object — those should still work but
        # surface a warning to nudge an upgrade.
        legacy = json.dumps({"env_vars": {"PGHOST": "db.example"}})
        with caplog.at_level(logging.WARNING):
            out = _decrypt_credentials_field(legacy)
        assert out == {"env_vars": {"PGHOST": "db.example"}}
        assert any("legacy plaintext" in r.message.lower() for r in caplog.records)

    def test_empty_legacy_plaintext_no_warning(self, caplog):
        # `{}` is the schema default — common and not worth a warning.
        with caplog.at_level(logging.WARNING):
            out = _decrypt_credentials_field("{}")
        assert out == {}
        assert not any("legacy plaintext" in r.message.lower() for r in caplog.records)

    def test_non_v1_string_returns_empty_with_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            out = _decrypt_credentials_field('"not-a-v1-ciphertext"')
        assert out == {}
        assert any("non-encrypted" in r.message.lower() for r in caplog.records)

    def test_malformed_json_returns_empty_with_error(self, caplog):
        with caplog.at_level(logging.ERROR):
            out = _decrypt_credentials_field("{not valid json")
        assert out == {}
        assert any("failed to parse" in r.message.lower() for r in caplog.records)

    def test_accepts_pre_parsed_dict_for_codec_compat(self):
        # Defensive: an asyncpg config that registers a JSONB codec would
        # hand us a dict directly. The helper still returns it (with the
        # legacy-plaintext warning).
        out = _decrypt_credentials_field({"already": "parsed"})
        assert out == {"already": "parsed"}


# =============================================================================
# _datasource_row_to_dict
# =============================================================================


class TestDatasourceRowToDict:
    def test_decrypts_credentials_and_passes_other_fields_through(self):
        encrypted = _encrypt_credentials_dict({"token": "secret"})
        row = {
            "id": "00000000-0000-0000-0000-000000000001",
            "name": "Cluster A",
            "type": "kubeconfig",
            "credentials": encrypted,
            "is_global": True,
        }
        out = _datasource_row_to_dict(row)
        assert out["credentials"] == {"token": "secret"}
        # Non-credential fields preserved verbatim.
        assert out["name"] == "Cluster A"
        assert out["type"] == "kubeconfig"
        assert out["is_global"] is True

    def test_handles_missing_credentials_field(self):
        # Some queries project a subset of columns and omit credentials —
        # the helper should not raise.
        row = {"id": "x", "name": "no-creds"}
        out = _datasource_row_to_dict(row)
        assert out == {"id": "x", "name": "no-creds"}


# =============================================================================
# Backfill semantics — pure-logic check, no DB
# =============================================================================


class TestBackfillLogicShape:
    """The DB-touching backfill in PostgresDB.backfill_encrypt_datasource_credentials
    branches on the same parsed-shape logic as _decrypt_credentials_field. These
    tests pin the three cases the backfill cares about so a future refactor
    doesn't silently break idempotency.
    """

    def test_already_encrypted_value_round_trips_to_itself_via_decrypt(self):
        # If a row is already encrypted, decrypting it returns the original
        # dict — confirming the backfill's "skip already-encrypted" branch
        # is observing the same shape we'd produce on a fresh write.
        original = {"env_vars": {"K": "v"}}
        encrypted = _encrypt_credentials_dict(original)
        parsed = json.loads(encrypted)
        assert isinstance(parsed, str)
        assert crypto.is_encrypted(parsed)
        assert _decrypt_credentials_field(encrypted) == original

    def test_legacy_plaintext_round_trip_through_encrypt(self):
        # A legacy plaintext row would be detected as a dict-after-json.loads,
        # re-encrypted, then later decrypted back. This pins the migration's
        # core invariant: backfill preserves contents.
        legacy_raw = json.dumps({"username": "u", "password": "p"})
        # Simulate what the backfill would do:
        parsed = json.loads(legacy_raw)
        assert isinstance(parsed, dict)
        new_value = _encrypt_credentials_dict(parsed)
        assert _decrypt_credentials_field(new_value) == {"username": "u", "password": "p"}
