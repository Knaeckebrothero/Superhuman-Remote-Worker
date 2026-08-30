"""GET /api/ssh/host-keys — publish the gateway's host keys for pinning.

Unauthenticated by design (see the `# nosec: public` marker above the route
in main.py): host keys are public material, and the client needs them
*before* it can authenticate anything else. Most of these tests call the
handler directly for speed; ``test_anonymous_http_get_reaches_the_handler``
goes through the real ASGI stack because unauthenticated *reachability* — not
just the inventory's classification of the route — is the property the whole
design rests on. The endpoint-inventory snapshot test
(tests/test_endpoint_inventory.py) proves the route is deliberately public
rather than merely unscoped; it must be run alongside this file, not instead
of it.
"""

import subprocess

import pytest
from fastapi.testclient import TestClient

import main


def _keygen(path):
    """Generate an ed25519 keypair at ``path``; return (private, public)."""
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(path)],
        check=True,
        capture_output=True,
    )
    return path, path.with_suffix(".pub")


def _blob(pub_path):
    """The base64 key blob from a ``.pub`` file, without type or comment."""
    return pub_path.read_text().split()[1]


@pytest.mark.asyncio
async def test_publishes_type_key_and_fingerprint(monkeypatch, tmp_path):
    key, pub = _keygen(tmp_path / "gw")
    fingerprint = subprocess.run(
        ["ssh-keygen", "-lf", str(pub)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()[1]

    monkeypatch.setenv("SSH_GATEWAY_PUBLIC_HOST_KEYS", str(pub))
    monkeypatch.setenv("SSH_GATEWAY_HOSTNAME", "ssh.srw.works")

    result = await main.get_ssh_host_keys(request=object())
    assert result["hostname"] == "ssh.srw.works"
    entry = result["host_keys"][0]
    assert entry["type"] == "ssh-ed25519"
    assert entry["fingerprint"].startswith("SHA256:")
    assert entry["public_key"].startswith("ssh-ed25519 ")
    # Tie the emitted material to *this* key. Without these two, a mutation
    # that published some other ed25519 key entirely satisfies every
    # assertion above.
    assert entry["public_key"].split()[1] == _blob(pub)
    assert entry["fingerprint"] == fingerprint


@pytest.mark.asyncio
async def test_never_emits_a_private_key(monkeypatch, tmp_path):
    """Pointing this at a private key file by mistake must never leak it.

    The task brief for this endpoint assumed ``asyncssh.import_public_key``
    raises on a private-key file, making ``entries`` empty and this test
    vacuous. That assumption is FALSE for asyncssh 2.24.0 (the version
    installed here) — verified both empirically and by reading
    ``asyncssh/public_key.py``'s ``_decode_public``: for OpenSSH-format
    private keys it decodes the unencrypted public half of the container,
    deliberately (the same thing ``ssh-keygen -y`` does). So ``entries``
    here is non-empty, not empty — asserting ``host_keys == []`` would be
    asserting something untrue and would fail against real asyncssh
    behavior.

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
    key, pub = _keygen(tmp_path / "gw")
    private_raw = key.read_text()
    expected_public_blob = _blob(pub)

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
async def test_one_unreadable_path_does_not_suppress_the_good_ones(
    monkeypatch, tmp_path
):
    """The documented per-path tolerance, with a second path to exercise it.

    ``get_ssh_host_keys``'s docstring promises "one bad entry in the list
    should not take down discovery for the rest". The plausible way to break
    that is hoisting the ``try`` outside the loop while tidying up the
    ``continue`` — which converts "skip the bad key" into "publish nothing"
    the first time an operator's list has a typo in it. Every single-path
    test stays green against that; this one does not.
    """
    good, good_pub = _keygen(tmp_path / "good")
    missing = tmp_path / "nope.pub"
    assert not missing.exists()

    monkeypatch.setenv("SSH_GATEWAY_PUBLIC_HOST_KEYS", f"{missing}, {good_pub}")
    monkeypatch.setenv("SSH_GATEWAY_HOSTNAME", "ssh.srw.works")

    result = await main.get_ssh_host_keys(request=object())

    assert len(result["host_keys"]) == 1
    assert result["host_keys"][0]["public_key"].split()[1] == _blob(good_pub)


@pytest.mark.asyncio
async def test_publishes_every_key_in_a_comma_separated_list(monkeypatch, tmp_path):
    """The variable is a *list* for a reason; publish all of it.

    Catches a ``break`` (or a ``return`` of only the first entry) after the
    first successful parse, which no single-path test can see, and confirms
    surrounding whitespace around a path is tolerated.
    """
    first, first_pub = _keygen(tmp_path / "first")
    second, second_pub = _keygen(tmp_path / "second")
    assert _blob(first_pub) != _blob(second_pub)

    monkeypatch.setenv("SSH_GATEWAY_PUBLIC_HOST_KEYS", f" {first_pub} , {second_pub} ")
    monkeypatch.setenv("SSH_GATEWAY_HOSTNAME", "ssh.srw.works")

    result = await main.get_ssh_host_keys(request=object())

    blobs = [entry["public_key"].split()[1] for entry in result["host_keys"]]
    assert blobs == [_blob(first_pub), _blob(second_pub)]


@pytest.mark.asyncio
async def test_a_partial_read_is_retried_not_cached(monkeypatch, tmp_path):
    """A transient read failure must not be pinned for the pod's lifetime.

    These files arrive on a projected Secret volume: the volume may not be
    mounted yet when the first request lands, and a read can fall in the
    window where kubelet swaps the ``..data`` symlink. The env value never
    changes across that, so memoizing the result of a failed read serves
    ``host_keys: []`` — a hard stop for a pinning client — until the pod
    restarts. Same env string, two calls, file appears in between: the
    second call must see it.
    """
    good, good_pub = _keygen(tmp_path / "good")
    late = tmp_path / "late.pub"
    paths = f"{good_pub},{late}"

    monkeypatch.setenv("SSH_GATEWAY_PUBLIC_HOST_KEYS", paths)
    monkeypatch.setenv("SSH_GATEWAY_HOSTNAME", "ssh.srw.works")

    degraded = await main.get_ssh_host_keys(request=object())
    assert len(degraded["host_keys"]) == 1

    _, late_source_pub = _keygen(tmp_path / "late_source")
    late.write_text(late_source_pub.read_text())

    recovered = await main.get_ssh_host_keys(request=object())
    assert len(recovered["host_keys"]) == 2, (
        "the failed read was cached under an env value that never changes"
    )
    assert [entry["public_key"].split()[1] for entry in recovered["host_keys"]] == [
        _blob(good_pub),
        _blob(late_source_pub),
    ]


@pytest.mark.asyncio
async def test_unconfigured_returns_an_empty_list(monkeypatch):
    monkeypatch.delenv("SSH_GATEWAY_PUBLIC_HOST_KEYS", raising=False)
    monkeypatch.delenv("SSH_GATEWAY_HOSTNAME", raising=False)
    result = await main.get_ssh_host_keys(request=object())
    assert result["host_keys"] == []
    assert result["hostname"] == ""


def test_anonymous_http_get_reaches_the_handler(monkeypatch, tmp_path):
    """No credentials, real ASGI stack, 200 with the key in the body.

    Every other test in this file bypasses routing and middleware by calling
    the handler with ``request=object()``, and the inventory snapshot proves
    only that the route is *classified* public. Neither shows that an
    anonymous ``GET`` actually arrives — which is the property the entire
    design rests on, since a client has nothing to authenticate with until
    it has these keys.
    """
    key, pub = _keygen(tmp_path / "gw")
    monkeypatch.setenv("SSH_GATEWAY_PUBLIC_HOST_KEYS", str(pub))
    monkeypatch.setenv("SSH_GATEWAY_HOSTNAME", "ssh.srw.works")

    response = TestClient(main.app, raise_server_exceptions=False).get(
        "/api/ssh/host-keys"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["hostname"] == "ssh.srw.works"
    assert [entry["public_key"].split()[1] for entry in body["host_keys"]] == [
        _blob(pub)
    ]
