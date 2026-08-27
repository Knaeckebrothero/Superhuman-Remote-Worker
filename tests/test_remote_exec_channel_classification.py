"""Unit tests for ``RemoteBackend._exec`` SSH-failure classification.

knowledge-base/knowledge/issues/maxsessions_parallel_tools_false_workspace_death.md (slice B): a
``ChannelException`` on a *live* transport is a per-connection session-limit
refusal from sshd (MaxSessions), not workspace death. It must be retried and,
if it persists, surface as an ordinary tool error — never as
``WorkspaceUnavailableError``, which triggers destructive pod recovery.
Transport-level failures (dead transport, socket errors, EOF) must keep
raising ``WorkspaceUnavailableError`` so real pod deaths still freeze fast.

These tests mock paramiko so no SSH is required (same harness as
tests/test_remote_forward_channel.py).
"""

import socket
import sys
from pathlib import Path
from unittest.mock import MagicMock

import paramiko
import pytest

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.core.backends.remote import RemoteBackend  # noqa: E402
from src.core.workspace_backend import (  # noqa: E402
    RemoteChannelBusyError,
    WorkspaceUnavailableError,
)


def _backend() -> RemoteBackend:
    return RemoteBackend(
        host="workspace-test.svc", port=30022, key_path=None, job_id="testjob01234"
    )


def _ok_exec_result(output: bytes = b"ok\n"):
    """Build the (stdin, stdout, stderr) triple for a successful exec."""
    chan = MagicMock(name="channel")
    reads = [output]

    def recv_ready():
        return bool(reads)

    def recv(_n):
        return reads.pop(0)

    chan.recv_ready.side_effect = recv_ready
    chan.recv.side_effect = recv
    chan.recv_stderr_ready.return_value = False
    chan.exit_status_ready.return_value = True
    chan.recv_exit_status.return_value = 0
    stdout = MagicMock(name="stdout")
    stdout.channel = chan
    return MagicMock(name="stdin"), stdout, MagicMock(name="stderr")


def _wire(be: RemoteBackend, exec_side_effect, transport_active: bool, monkeypatch):
    transport = MagicMock(name="transport")
    transport.is_active.return_value = transport_active
    ssh = MagicMock(name="ssh")
    ssh.get_transport.return_value = transport
    ssh.exec_command.side_effect = exec_side_effect
    be._ssh = ssh
    monkeypatch.setattr(be, "_ensure_connected", lambda: None)
    # Retry backoff must not slow the suite down.
    monkeypatch.setattr("src.core.backends.remote.time.sleep", lambda _s: None)
    return ssh


def test_channel_refusal_on_live_transport_is_retried_to_success(monkeypatch):
    be = _backend()
    ssh = _wire(
        be,
        [paramiko.ChannelException(2, "Connect failed"), _ok_exec_result()],
        transport_active=True,
        monkeypatch=monkeypatch,
    )

    out = be._exec("echo ok")

    assert out == "ok\n"
    assert ssh.exec_command.call_count == 2


def test_persistent_channel_refusal_on_live_transport_is_not_workspace_death(
    monkeypatch,
):
    be = _backend()
    ssh = _wire(
        be,
        paramiko.ChannelException(2, "Connect failed"),
        transport_active=True,
        monkeypatch=monkeypatch,
    )

    with pytest.raises(RemoteChannelBusyError):
        be._exec("echo ok")

    # 1 initial + 3 retries
    assert ssh.exec_command.call_count == 4


def test_channel_exception_on_dead_transport_is_workspace_death(monkeypatch):
    be = _backend()
    ssh = _wire(
        be,
        paramiko.ChannelException(2, "Connect failed"),
        transport_active=False,
        monkeypatch=monkeypatch,
    )

    with pytest.raises(WorkspaceUnavailableError):
        be._exec("echo ok")

    # Dead transport is not retried — fail fast into recovery.
    assert ssh.exec_command.call_count == 1


def test_plain_ssh_exception_still_raises_workspace_death(monkeypatch):
    be = _backend()
    _wire(
        be,
        paramiko.SSHException("Server connection dropped"),
        transport_active=True,
        monkeypatch=monkeypatch,
    )

    with pytest.raises(WorkspaceUnavailableError):
        be._exec("echo ok")


def test_socket_error_still_raises_workspace_death(monkeypatch):
    be = _backend()
    _wire(
        be,
        socket.error("connection reset"),
        transport_active=True,
        monkeypatch=monkeypatch,
    )

    with pytest.raises(WorkspaceUnavailableError):
        be._exec("echo ok")
