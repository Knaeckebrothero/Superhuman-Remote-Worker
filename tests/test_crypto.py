"""Tests for orchestrator.security.crypto."""

from __future__ import annotations

import base64
import secrets

import pytest

from orchestrator.security import crypto


def _set_key(monkeypatch, raw: str | None) -> None:
    if raw is None:
        monkeypatch.delenv(crypto.ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(crypto.ENV_VAR, raw)
    crypto.reset_cipher_cache()


@pytest.fixture
def key_b64(monkeypatch):
    raw = base64.b64encode(secrets.token_bytes(32)).decode()
    _set_key(monkeypatch, raw)
    yield raw
    crypto.reset_cipher_cache()


class TestRoundTrip:
    def test_round_trip_preserves_plaintext(self, key_b64):
        plaintext = "sk-proj-abcdefghijklmnop"
        ct = crypto.encrypt(plaintext)
        assert crypto.is_encrypted(ct)
        assert crypto.decrypt(ct) == plaintext

    def test_unicode_round_trip(self, key_b64):
        plaintext = "héllo 🔑 мир"
        assert crypto.decrypt(crypto.encrypt(plaintext)) == plaintext

    def test_each_call_uses_fresh_nonce(self, key_b64):
        c1 = crypto.encrypt("same-value")
        c2 = crypto.encrypt("same-value")
        assert c1 != c2
        assert crypto.decrypt(c1) == crypto.decrypt(c2) == "same-value"


class TestKeyFormats:
    def test_urlsafe_base64_key_accepted(self, monkeypatch):
        raw = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
        _set_key(monkeypatch, raw)
        assert crypto.decrypt(crypto.encrypt("ok")) == "ok"

    def test_raw_32_char_key_accepted(self, monkeypatch):
        # Matches Helm's randAlphaNum 32 output shape.
        _set_key(monkeypatch, "a" * 32)
        assert crypto.decrypt(crypto.encrypt("ok")) == "ok"

    def test_missing_env_raises(self, monkeypatch):
        _set_key(monkeypatch, None)
        with pytest.raises(crypto.EncryptionKeyError):
            crypto.encrypt("x")

    def test_empty_key_raises(self, monkeypatch):
        _set_key(monkeypatch, "")
        with pytest.raises(crypto.EncryptionKeyError):
            crypto.encrypt("x")

    def test_wrong_length_key_raises(self, monkeypatch):
        _set_key(monkeypatch, base64.b64encode(b"too-short").decode())
        with pytest.raises(crypto.EncryptionKeyError):
            crypto.encrypt("x")


class TestEncryptGuards:
    def test_empty_plaintext_rejected(self, key_b64):
        with pytest.raises(ValueError):
            crypto.encrypt("")

    def test_non_string_plaintext_rejected(self, key_b64):
        with pytest.raises(TypeError):
            crypto.encrypt(123)  # type: ignore[arg-type]


class TestDecryptGuards:
    def test_legacy_plaintext_rejected(self, key_b64):
        with pytest.raises(crypto.DecryptionError):
            crypto.decrypt("sk-legacy-plaintext-value")

    def test_malformed_structure_rejected(self, key_b64):
        with pytest.raises(crypto.DecryptionError):
            crypto.decrypt("v1:onlyonepart")

    def test_bad_base64_rejected(self, key_b64):
        with pytest.raises(crypto.DecryptionError):
            crypto.decrypt("v1:!!!:???")

    def test_tampered_ciphertext_rejected(self, key_b64):
        ct = crypto.encrypt("sk-original")
        tampered = ct[:-4] + "AAAA"
        with pytest.raises(crypto.DecryptionError):
            crypto.decrypt(tampered)

    def test_wrong_key_cannot_decrypt(self, monkeypatch):
        raw1 = base64.b64encode(secrets.token_bytes(32)).decode()
        _set_key(monkeypatch, raw1)
        ct = crypto.encrypt("sk-value")
        raw2 = base64.b64encode(secrets.token_bytes(32)).decode()
        _set_key(monkeypatch, raw2)
        with pytest.raises(crypto.DecryptionError):
            crypto.decrypt(ct)


class TestIsEncrypted:
    def test_detects_v1_prefix(self, key_b64):
        assert crypto.is_encrypted(crypto.encrypt("x"))

    def test_rejects_plaintext(self):
        assert not crypto.is_encrypted("sk-plain")

    def test_rejects_none(self):
        assert not crypto.is_encrypted(None)
