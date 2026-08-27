"""Unit tests for the RemoteBackend SSH session-channel semaphore.

knowledge-base/knowledge/issues/maxsessions_parallel_tools_false_workspace_death.md (slice A): a
wide parallel tool batch must never open more concurrent session channels than
the workspace sshd allows (MaxSessions). The backend bounds short-lived exec
channels with a semaphore (default 10, ``WORKSPACE_SSH_MAX_CONCURRENT_CHANNELS``
override); excess execs queue instead of being refused. Long-lived channels
(persistent SFTP, shell tabs) are headroom, not semaphore-managed.

Mocked paramiko; concurrency is exercised with real threads.
"""

import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.core.backends.remote import RemoteBackend  # noqa: E402


def _backend() -> RemoteBackend:
    return RemoteBackend(
        host="workspace-test.svc", port=30022, key_path=None, job_id="testjob01234"
    )


class _ChannelTracker:
    """Fake exec_command factory that measures peak concurrent open channels.

    A channel counts as open from ``exec_command`` until ``chan.close()``.
    Each fake command "runs" for ``hold_seconds`` before reporting exit.
    """

    def __init__(self, hold_seconds: float = 0.03):
        self._hold = hold_seconds
        self._lock = threading.Lock()
        self._active = 0
        self.peak = 0

    def exec_command(self, _command, timeout=None):
        with self._lock:
            self._active += 1
            self.peak = max(self.peak, self._active)
        done_at = time.monotonic() + self._hold
        chan = MagicMock(name="channel")
        chan.recv_ready.return_value = False
        chan.recv_stderr_ready.return_value = False
        chan.exit_status_ready.side_effect = lambda: time.monotonic() >= done_at
        chan.recv_exit_status.return_value = 0

        def close():
            with self._lock:
                self._active -= 1

        chan.close.side_effect = close
        stdout = MagicMock(name="stdout")
        stdout.channel = chan
        return MagicMock(name="stdin"), stdout, MagicMock(name="stderr")


def _wire(be: RemoteBackend, tracker: _ChannelTracker, monkeypatch):
    transport = MagicMock(name="transport")
    transport.is_active.return_value = True
    ssh = MagicMock(name="ssh")
    ssh.get_transport.return_value = transport
    ssh.exec_command.side_effect = tracker.exec_command
    be._ssh = ssh
    monkeypatch.setattr(be, "_ensure_connected", lambda: None)


def _run_parallel_execs(be: RemoteBackend, n: int):
    errors: list = []

    def run():
        try:
            be._exec("true", timeout=10)
        except Exception as e:  # pragma: no cover - failure detail for assert
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors


def test_concurrent_execs_bounded_by_default_cap(monkeypatch):
    be = _backend()
    tracker = _ChannelTracker()
    _wire(be, tracker, monkeypatch)

    _run_parallel_execs(be, 25)

    assert tracker.peak <= 10


def test_cap_honours_env_override(monkeypatch):
    monkeypatch.setenv("WORKSPACE_SSH_MAX_CONCURRENT_CHANNELS", "2")
    be = _backend()
    tracker = _ChannelTracker()
    _wire(be, tracker, monkeypatch)

    _run_parallel_execs(be, 8)

    assert tracker.peak <= 2


def test_successful_exec_closes_its_channel(monkeypatch):
    # Explicit close frees the server-side MaxSessions slot promptly; without
    # it the slot lingers until GC and the semaphore's promise is hollow.
    be = _backend()
    tracker = _ChannelTracker(hold_seconds=0.0)
    _wire(be, tracker, monkeypatch)

    be._exec("true", timeout=10)

    assert tracker._active == 0
