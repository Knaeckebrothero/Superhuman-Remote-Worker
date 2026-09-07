"""Unit tests for ``RemoteBackend.open_forward_channel``.

This is the reusable tunnel primitive behind live-VM IDE access
(knowledge-base/knowledge/features/vm_snapshots_and_ide.md, "Live-VM IDE Access via the Agent"):
the agent opens a ``direct-tcpip`` channel over its existing authenticated SSH
transport to reach code-server on the VM's loopback, which is not exposed on
the mesh network. These tests mock paramiko so no SSH/VM is required.
"""

from unittest.mock import MagicMock

import pytest

from shared.runtime.core.backends.remote import RemoteBackend  # noqa: E402
from shared.runtime.core.workspace_backend import WorkspaceUnavailableError  # noqa: E402


def _backend() -> RemoteBackend:
    return RemoteBackend(
        host="100.64.25.224", port=22, key_path=None, job_id="testjob01234"
    )


def test_open_forward_channel_opens_direct_tcpip_to_loopback(monkeypatch):
    be = _backend()
    channel = MagicMock(name="channel")
    transport = MagicMock(name="transport")
    transport.is_active.return_value = True
    transport.open_channel.return_value = channel
    ssh = MagicMock(name="ssh")
    ssh.get_transport.return_value = transport
    be._ssh = ssh
    # Don't touch the network — the transport is already "connected".
    monkeypatch.setattr(be, "_ensure_connected", lambda: None)

    result = be.open_forward_channel(dest_port=8080)

    assert result is channel
    transport.open_channel.assert_called_once_with(
        "direct-tcpip", ("127.0.0.1", 8080), ("127.0.0.1", 0)
    )


def test_open_forward_channel_honours_custom_dest(monkeypatch):
    be = _backend()
    transport = MagicMock()
    transport.is_active.return_value = True
    ssh = MagicMock()
    ssh.get_transport.return_value = transport
    be._ssh = ssh
    monkeypatch.setattr(be, "_ensure_connected", lambda: None)

    be.open_forward_channel(dest_host="127.0.0.1", dest_port=1234)

    transport.open_channel.assert_called_once_with(
        "direct-tcpip", ("127.0.0.1", 1234), ("127.0.0.1", 0)
    )


def test_open_forward_channel_raises_when_transport_inactive(monkeypatch):
    be = _backend()
    transport = MagicMock()
    transport.is_active.return_value = False
    ssh = MagicMock()
    ssh.get_transport.return_value = transport
    be._ssh = ssh
    monkeypatch.setattr(be, "_ensure_connected", lambda: None)

    with pytest.raises(WorkspaceUnavailableError):
        be.open_forward_channel()


def test_open_forward_channel_raises_when_no_transport(monkeypatch):
    be = _backend()
    ssh = MagicMock()
    ssh.get_transport.return_value = None
    be._ssh = ssh
    monkeypatch.setattr(be, "_ensure_connected", lambda: None)

    with pytest.raises(WorkspaceUnavailableError):
        be.open_forward_channel()
