"""Safety checks for the opt-in, disposable native k3d runtime gate."""

import errno
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import httpx
import pytest


@pytest.fixture
def gate():
    path = Path(__file__).resolve().parents[1] / "scripts/manifests-native-k3d-gate.py"
    spec = importlib.util.spec_from_file_location("native_manifest_gate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_gate_refuses_nonlocal_context_before_inspection_or_publication(
    gate, monkeypatch
):
    monkeypatch.setattr(
        gate.config,
        "new_client_from_config",
        lambda **kwargs: SimpleNamespace(
            configuration=SimpleNamespace(host="https://production.example")
        ),
    )
    monkeypatch.setattr(
        gate.client,
        "CoreV1Api",
        lambda *args: pytest.fail("must not inspect external cluster"),
    )
    with pytest.raises(gate.GateFailure, match="local API endpoint"):
        gate.verify_cluster()


def test_gate_ledger_is_reconstructed_from_committed_state(gate, tmp_path):
    path = tmp_path / "identities.sqlite3"
    gate.Ledger(path).write(
        "instance", pvc_uid="recorded-pvc", initialized=True, state="Detached"
    )
    assert gate.Ledger(path).read("instance") == {
        "pvc_uid": "recorded-pvc",
        "initialized": True,
        "state": "Detached",
    }


@pytest.mark.parametrize("wrong_digest", [False, True])
def test_registry_bytes_determine_published_digest_not_local_image_index(
    gate, monkeypatch, wrong_digest
):
    local_index = "sha256:" + "1" * 64
    manifest = (
        b'{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json"}'
    )
    digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
    publications = []

    def command(args, **kwargs):
        if args[:3] == ["docker", "image", "inspect"]:
            return json.dumps(
                [
                    {
                        "Id": local_index,
                        "RepoDigests": [
                            "localhost:5005/srw-native-gate-busybox@" + local_index
                        ],
                    }
                ]
            )
        if args[:2] == ["docker", "run"]:
            return (
                hashlib.sha256(
                    (gate.ROOT / "docker/workspace-entrypoint.sh").read_bytes()
                ).hexdigest()
                + " entrypoint.sh"
            )
        publications.append(args)
        return ""

    def respond(request):
        assert request.url.host == "localhost" and request.url.port == 5005
        assert "application/vnd.oci.image.manifest.v1+json" in request.headers["accept"]
        return httpx.Response(
            200,
            content=manifest,
            headers={"Docker-Content-Digest": local_index if wrong_digest else digest},
        )

    monkeypatch.setattr(gate, "command", command)
    monkeypatch.setattr(
        gate.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0)
    )
    real_client = httpx.Client
    monkeypatch.setattr(
        gate.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    args = SimpleNamespace(
        workspace_image="workspace:test",
        busybox_image="busybox:test",
        ssh_image="ssh:test",
        python_image="python:test",
    )
    if wrong_digest:
        with pytest.raises(gate.GateFailure, match="manifest bytes"):
            gate.publish_images(args)
    else:
        images, identities = gate.publish_images(args)
        assert all(value.endswith("@" + digest) for value in images.values())
        assert all(
            value["localImageID"] == local_index and value["registryDigest"] == digest
            for value in identities.values()
        )
        assert len(publications) == 8


@pytest.mark.parametrize(
    "error_number,expected", [(errno.EHOSTUNREACH, 0), (errno.EBADF, 11)]
)
def test_network_negative_control_accepts_cni_reject_but_rejects_probe_errors(
    gate, monkeypatch, error_number, expected
):
    def connection(*args, **kwargs):
        raise OSError(error_number, "test error")

    monkeypatch.setattr(socket, "create_connection", connection)
    monkeypatch.setattr(gate.time, "sleep", lambda _: None)
    with pytest.raises(SystemExit) as outcome:
        exec(gate.connection_probe("192.0.2.1", 9443, allowed=False), {})
    assert outcome.value.code == expected


def test_gate_never_prints_an_arbitrary_exception_body(gate, monkeypatch, capsys):
    async def failed(_):
        raise RuntimeError("secret-value-from-untrusted-api-body")

    monkeypatch.setattr(gate, "run", failed)
    monkeypatch.setattr("sys.argv", ["manifests-native-k3d-gate.py"])
    assert gate.main() == 1
    output = capsys.readouterr().out
    assert "RuntimeError" in output
    assert "secret-value" not in output
