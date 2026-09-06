"""Tests for :mod:`services.ssh_public_keys`.

Pure, offline coverage: key-type/strength policy, private-key/garbage
rejection, the SHA256 fingerprint format (must be byte-identical to
``ssh-keygen -lf``, since that is what a future ssh-gateway will present to
the orchestrator), and possession verification via SSHSIG.
"""

import subprocess

import pytest

from orchestrator.services.ssh_public_keys import (
    SIGNATURE_NAMESPACE,
    ParsedSshKey,
    SshKeyRejected,
    parse_public_key,
    verify_possession,
)


def _keygen(tmp_path, key_type, extra=()):
    path = tmp_path / "k"
    subprocess.run(
        [
            "ssh-keygen",
            "-t",
            key_type,
            "-N",
            "",
            "-C",
            "test@srw",
            "-f",
            str(path),
            *extra,
        ],
        check=True,
        capture_output=True,
    )
    return path


def test_accepts_ed25519(tmp_path):
    path = _keygen(tmp_path, "ed25519")
    parsed = parse_public_key((path.with_suffix(".pub")).read_text())
    assert isinstance(parsed, ParsedSshKey)
    assert parsed.key_type == "ssh-ed25519"
    assert parsed.fingerprint_sha256.startswith("SHA256:")
    assert parsed.comment == "test@srw"


def test_accepts_rsa_3072(tmp_path):
    path = _keygen(tmp_path, "rsa", extra=["-b", "3072"])
    parsed = parse_public_key((path.with_suffix(".pub")).read_text())
    assert parsed.key_type == "ssh-rsa"


def test_rejects_rsa_2048(tmp_path):
    path = _keygen(tmp_path, "rsa", extra=["-b", "2048"])
    with pytest.raises(SshKeyRejected) as excinfo:
        parse_public_key((path.with_suffix(".pub")).read_text())
    assert "3072" in excinfo.value.reason


def test_rejects_garbage():
    with pytest.raises(SshKeyRejected):
        parse_public_key("not a key")


def test_rejects_private_key_paste(tmp_path):
    """Users paste the wrong file. Say so, do not store it."""
    path = _keygen(tmp_path, "ed25519")
    with pytest.raises(SshKeyRejected) as excinfo:
        parse_public_key(path.read_text())
    assert "private" in excinfo.value.reason.lower()


def test_fingerprint_matches_ssh_keygen(tmp_path):
    """Our fingerprint must equal what the user sees locally, or support breaks."""
    path = _keygen(tmp_path, "ed25519")
    parsed = parse_public_key((path.with_suffix(".pub")).read_text())
    out = subprocess.run(
        ["ssh-keygen", "-lf", str(path.with_suffix(".pub"))],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert parsed.fingerprint_sha256 == out.split()[1]


def test_verify_possession_round_trip(tmp_path):
    path = _keygen(tmp_path, "ed25519")
    payload = b"srw-challenge-abc123"
    (tmp_path / "payload").write_bytes(payload)
    subprocess.run(
        [
            "ssh-keygen",
            "-Y",
            "sign",
            "-f",
            str(path),
            "-n",
            SIGNATURE_NAMESPACE,
            str(tmp_path / "payload"),
        ],
        check=True,
        capture_output=True,
    )
    signature = (tmp_path / "payload.sig").read_text()
    public_key = (path.with_suffix(".pub")).read_text()
    assert (
        verify_possession(public_key, SIGNATURE_NAMESPACE, payload, signature) is True
    )


def test_verify_possession_rejects_wrong_payload(tmp_path):
    path = _keygen(tmp_path, "ed25519")
    (tmp_path / "payload").write_bytes(b"srw-challenge-abc123")
    subprocess.run(
        [
            "ssh-keygen",
            "-Y",
            "sign",
            "-f",
            str(path),
            "-n",
            SIGNATURE_NAMESPACE,
            str(tmp_path / "payload"),
        ],
        check=True,
        capture_output=True,
    )
    signature = (tmp_path / "payload.sig").read_text()
    public_key = (path.with_suffix(".pub")).read_text()
    assert (
        verify_possession(public_key, SIGNATURE_NAMESPACE, b"different", signature)
        is False
    )


def test_verify_possession_rejects_other_key(tmp_path):
    signer = _keygen(tmp_path, "ed25519")
    victim_dir = tmp_path / "victim"
    victim_dir.mkdir()
    other = _keygen(victim_dir, "ed25519")
    (tmp_path / "payload").write_bytes(b"srw-challenge-abc123")
    subprocess.run(
        [
            "ssh-keygen",
            "-Y",
            "sign",
            "-f",
            str(signer),
            "-n",
            SIGNATURE_NAMESPACE,
            str(tmp_path / "payload"),
        ],
        check=True,
        capture_output=True,
    )
    signature = (tmp_path / "payload.sig").read_text()
    victim_public_key = (other.with_suffix(".pub")).read_text()
    assert (
        verify_possession(
            victim_public_key, SIGNATURE_NAMESPACE, b"srw-challenge-abc123", signature
        )
        is False
    )
