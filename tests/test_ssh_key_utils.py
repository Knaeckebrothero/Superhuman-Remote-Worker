"""Tests for :mod:`src.utils.ssh_key`.

Covers the formatting bug we hit in production: a private key stored without
a trailing newline made OpenSSL/libcrypto reject it. The normalizer must add
exactly one trailing newline regardless of how the input was massaged on the
way in, and the validator must accept well-formed PEM keys while catching
common corruption shapes.
"""

from __future__ import annotations

import pytest

from src.utils.ssh_key import (
    GeneratedKeypair,
    InvalidSSHKeyError,
    generate_ed25519_keypair,
    normalize_private_key,
    validate_private_key,
)


# A real ed25519 key with no passphrase, comment "test-fixture@srw".
# Used as a known-good baseline for round-trip tests.
VALID_ED25519 = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
    "QyNTUxOQAAACAK89GbliOywfgVW2OxznwGs84eJxJE4IZ9+Eu8mrO1RwAAAJhvDkS7bw5E\n"
    "uwAAAAtzc2gtZWQyNTUxOQAAACAK89GbliOywfgVW2OxznwGs84eJxJE4IZ9+Eu8mrO1Rw\n"
    "AAAEB93iLMaUtuQZDbUIaCFdgKNO0trRtioluKbeoG6OSjkwrz0ZuWI7LB+BVbY7HOfAaz\n"
    "zh4nEkTghn34S7yas7VHAAAAEHRlc3QtZml4dHVyZUBzcncBAgMEBQ==\n"
    "-----END OPENSSH PRIVATE KEY-----\n"
)


class TestNormalize:
    def test_passthrough_when_already_normal(self):
        assert normalize_private_key(VALID_ED25519) == VALID_ED25519

    def test_adds_trailing_newline_when_missing(self):
        """The exact bug from the incident: no trailing \\n."""
        stripped = VALID_ED25519.rstrip("\n")
        assert not stripped.endswith("\n")
        assert normalize_private_key(stripped) == VALID_ED25519

    def test_collapses_multiple_trailing_newlines(self):
        padded = VALID_ED25519 + "\n\n\n"
        assert normalize_private_key(padded) == VALID_ED25519

    def test_strips_leading_whitespace(self):
        leading = "   \n\t" + VALID_ED25519
        assert normalize_private_key(leading) == VALID_ED25519

    def test_converts_crlf_to_lf(self):
        crlf = VALID_ED25519.replace("\n", "\r\n")
        assert "\r" not in normalize_private_key(crlf)
        assert normalize_private_key(crlf) == VALID_ED25519

    def test_converts_lone_cr_to_lf(self):
        cr_only = VALID_ED25519.replace("\n", "\r")
        assert normalize_private_key(cr_only) == VALID_ED25519

    def test_empty_string_passes_through(self):
        assert normalize_private_key("") == ""

    def test_whitespace_only_becomes_empty(self):
        assert normalize_private_key("   \n\n  ") == ""

    def test_non_string_returned_unchanged(self):
        # Non-strings are the caller's problem to validate — normalize is
        # tolerant so it can sit on the consume path without raising.
        assert normalize_private_key(None) is None  # type: ignore[arg-type]


class TestValidate:
    def test_accepts_valid_ed25519(self):
        assert validate_private_key(VALID_ED25519) == VALID_ED25519

    def test_repairs_missing_trailing_newline(self):
        """The validator is the save-time gate; it must accept (and
        return normalized) the same broken-but-recoverable shape that
        bit us in prod."""
        broken = VALID_ED25519.rstrip("\n")
        repaired = validate_private_key(broken)
        assert repaired == VALID_ED25519
        assert repaired.endswith("\n")

    def test_repairs_crlf_endings(self):
        repaired = validate_private_key(VALID_ED25519.replace("\n", "\r\n"))
        assert repaired == VALID_ED25519

    def test_rejects_empty(self):
        with pytest.raises(InvalidSSHKeyError, match="empty"):
            validate_private_key("")

    def test_rejects_whitespace_only(self):
        with pytest.raises(InvalidSSHKeyError, match="empty"):
            validate_private_key("   \n\n  ")

    def test_rejects_non_string(self):
        with pytest.raises(InvalidSSHKeyError):
            validate_private_key(None)  # type: ignore[arg-type]
        with pytest.raises(InvalidSSHKeyError):
            validate_private_key(b"-----BEGIN OPENSSH PRIVATE KEY-----")  # type: ignore[arg-type]

    def test_rejects_missing_begin_marker(self):
        body = "\n".join(VALID_ED25519.splitlines()[1:])
        with pytest.raises(InvalidSSHKeyError, match="BEGIN marker"):
            validate_private_key(body)

    def test_rejects_missing_end_marker(self):
        body = "\n".join(VALID_ED25519.splitlines()[:-1])
        with pytest.raises(InvalidSSHKeyError, match="END marker"):
            validate_private_key(body)

    def test_rejects_mismatched_markers(self):
        mismatched = VALID_ED25519.replace(
            "-----END OPENSSH PRIVATE KEY-----",
            "-----END RSA PRIVATE KEY-----",
        )
        with pytest.raises(InvalidSSHKeyError, match="does not match"):
            validate_private_key(mismatched)

    def test_rejects_unrecognized_marker(self):
        with pytest.raises(InvalidSSHKeyError, match="BEGIN marker"):
            validate_private_key(
                "-----BEGIN MY HOMEMADE KEY-----\nQUJD\n-----END MY HOMEMADE KEY-----\n"
            )

    def test_rejects_garbage_body(self):
        bad = (
            "-----BEGIN OPENSSH PRIVATE KEY-----\n"
            "not valid base64 !@#$%\n"
            "-----END OPENSSH PRIVATE KEY-----\n"
        )
        with pytest.raises(InvalidSSHKeyError, match="base64 body"):
            validate_private_key(bad)

    def test_rejects_truncated_body(self):
        # Drop the middle base64 line so b64decode fails on the trailing bits.
        lines = VALID_ED25519.splitlines()
        truncated = "\n".join(lines[:2] + [lines[2][:5]] + lines[-1:]) + "\n"
        with pytest.raises(InvalidSSHKeyError):
            validate_private_key(truncated)

    def test_accepts_rsa_marker_pair(self):
        # Just structural check — body is a base64 placeholder, not a real key.
        rsa_like = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "QUJDREVGRw==\n"
            "-----END RSA PRIVATE KEY-----\n"
        )
        assert validate_private_key(rsa_like) == rsa_like

    def test_idempotent_on_valid_input(self):
        once = validate_private_key(VALID_ED25519)
        twice = validate_private_key(once)
        assert once == twice == VALID_ED25519


class TestGenerateEd25519Keypair:
    def test_returns_named_tuple_shape(self):
        kp = generate_ed25519_keypair()
        assert isinstance(kp, GeneratedKeypair)
        assert isinstance(kp.private_key, str)
        assert isinstance(kp.public_key, str)

    def test_private_key_validates(self):
        """Round-trip the generated key through the validator we run on
        save. Catches any regression where the generator emits a shape
        the API would reject."""
        kp = generate_ed25519_keypair()
        validated = validate_private_key(kp.private_key)
        assert validated == kp.private_key

    def test_private_key_has_single_trailing_newline(self):
        kp = generate_ed25519_keypair()
        assert kp.private_key.endswith("\n")
        assert not kp.private_key.endswith("\n\n")

    def test_private_key_has_openssh_markers(self):
        kp = generate_ed25519_keypair()
        assert kp.private_key.startswith("-----BEGIN OPENSSH PRIVATE KEY-----\n")
        assert kp.private_key.rstrip("\n").endswith("-----END OPENSSH PRIVATE KEY-----")

    def test_public_key_is_single_line(self):
        kp = generate_ed25519_keypair()
        assert "\n" not in kp.public_key
        assert kp.public_key.startswith("ssh-ed25519 ")

    def test_public_key_carries_comment(self):
        kp = generate_ed25519_keypair(comment="my-repo (srw)")
        # Comment should be the last whitespace-separated token group.
        assert kp.public_key.endswith(" my-repo (srw)")

    def test_public_key_omits_empty_comment(self):
        kp = generate_ed25519_keypair(comment="")
        # ssh-ed25519 <base64> — exactly two whitespace-separated tokens.
        assert len(kp.public_key.split(" ")) == 2

    def test_public_key_sanitizes_comment(self):
        # Newlines or tabs in the comment would break the single-line format.
        kp = generate_ed25519_keypair(comment="line1\nline2\tx")
        assert "\n" not in kp.public_key
        assert "\t" not in kp.public_key

    def test_two_generations_differ(self):
        a = generate_ed25519_keypair()
        b = generate_ed25519_keypair()
        assert a.private_key != b.private_key
        assert a.public_key != b.public_key
