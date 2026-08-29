"""GET /api/ssh/host-keys — publish the gateway's host keys for pinning.

Unauthenticated by design (see the `# nosec: public` marker above the route
in main.py): host keys are public material, and the client needs them
*before* it can authenticate anything else. These tests call the handler
directly, exactly like the routing-bypassing tests already in this file did
before this docstring was written — the endpoint-inventory snapshot test
(tests/test_endpoint_inventory.py) is what actually proves the route itself
is deliberately public rather than merely unscoped; it must be run alongside
this file, not instead of it.
"""

import pytest

import main


@pytest.mark.asyncio
async def test_publishes_type_key_and_fingerprint(monkeypatch, tmp_path):
    import subprocess

    key = tmp_path / "gw"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv("SSH_GATEWAY_PUBLIC_HOST_KEYS", str(key.with_suffix(".pub")))
    monkeypatch.setenv("SSH_GATEWAY_HOSTNAME", "ssh.srw.works")

    result = await main.get_ssh_host_keys(request=object())
    assert result["hostname"] == "ssh.srw.works"
    entry = result["host_keys"][0]
    assert entry["type"] == "ssh-ed25519"
    assert entry["fingerprint"].startswith("SHA256:")
    assert entry["public_key"].startswith("ssh-ed25519 ")


@pytest.mark.asyncio
async def test_never_emits_a_private_key(monkeypatch, tmp_path):
    """Pointing this at a private key file by mistake must never leak it.

    The task brief for this endpoint assumed ``asyncssh.import_public_key``
    raises on a private-key file, making ``entries`` empty and this test
    vacuous. That assumption is FALSE for asyncssh 2.24.0 (the version
    installed here) — verified both empirically and by reading
    ``asyncssh/public_key.py``'s ``_decode_public``: when the primary parse
    fails, it falls back to decoding the data as a PRIVATE key and calling
    ``.convert_to_public()`` on it, deliberately (the same thing
    ``ssh-keygen -y`` does). So ``entries`` here is non-empty, not empty —
    asserting ``host_keys == []`` would be asserting something untrue and
    would fail against real asyncssh behavior.

    The actual safety property is not "the entry gets rejected" (it
    doesn't) but "nothing private ever reaches the response": only
    ``export_public_key()`` output should appear, and it should be
    byte-for-byte the same public key material as the key's own ``.pub``
    file — never the raw private-key blob or any of its markers. This is
    the regression that would actually happen if a future edit swapped in
    the raw file content or an export-private call: this test fails against
    that mutation (checked directly, not assumed) where the vacuous
    original would not have.
    """
    import subprocess

    key = tmp_path / "gw"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
        capture_output=True,
    )
    private_raw = key.read_text()
    expected_public_blob = key.with_suffix(".pub").read_text().split()[1]

    monkeypatch.setenv("SSH_GATEWAY_PUBLIC_HOST_KEYS", str(key))
    monkeypatch.setenv("SSH_GATEWAY_HOSTNAME", "ssh.srw.works")

    result = await main.get_ssh_host_keys(request=object())

    assert result["host_keys"], (
        "expected asyncssh to leniently extract the public component from "
        "this private-key file (see docstring); if this is now empty, "
        "asyncssh's parsing behavior changed and this test's premise needs "
        "re-verifying against the installed version"
    )
    for entry in result["host_keys"]:
        assert "PRIVATE" not in entry["public_key"]
        assert "BEGIN OPENSSH PRIVATE KEY" not in entry["public_key"]
        assert private_raw.strip() not in entry["public_key"]
        assert entry["public_key"].split()[1] == expected_public_blob


@pytest.mark.asyncio
async def test_unconfigured_returns_an_empty_list(monkeypatch):
    monkeypatch.delenv("SSH_GATEWAY_PUBLIC_HOST_KEYS", raising=False)
    result = await main.get_ssh_host_keys(request=object())
    assert result["host_keys"] == []
