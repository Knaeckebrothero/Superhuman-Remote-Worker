"""Tests for the RemoteBackend workspace implementation.

RemoteBackend operates over SSH/SFTP via paramiko. The tests mock paramiko
entirely to avoid requiring SSH infrastructure.
"""

import errno
import io
import socket
import stat as stat_module
from contextlib import contextmanager
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add project root to path
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.core.workspace_backend import (  # noqa: E402
    RemoteCommandTimeoutError,
    WorkspaceUnavailableError,
)


# =============================================================================
# Helpers
# =============================================================================


def _make_sftp_attr(*, is_dir: bool = False, size: int = 0) -> MagicMock:
    """Create a mock SFTPAttributes object with proper st_mode."""
    attr = MagicMock()
    if is_dir:
        attr.st_mode = stat_module.S_IFDIR | 0o755
    else:
        attr.st_mode = stat_module.S_IFREG | 0o644
    attr.st_size = size
    return attr


def _make_sftp_entry(
    filename: str, *, is_dir: bool = False, size: int = 0
) -> MagicMock:
    """Create a mock SFTP directory entry (listdir_attr result)."""
    entry = _make_sftp_attr(is_dir=is_dir, size=size)
    entry.filename = filename
    return entry


class _WindowedChannel:
    """Mock paramiko Channel with window-full deadlock semantics.

    Exit status only becomes ready once ALL pending output has been
    recv()'d — exactly like a remote command blocked on pipe_write until
    the reader drains the channel. recv_exit_status() raises if called
    while output is undrained, which is the deadlock the fix removes
    (on real paramiko it blocks forever instead of raising).
    """

    def __init__(
        self, stdout_data=b"", stderr_data=b"", exit_code=0, never_exits=False
    ):
        self._out = stdout_data
        self._err = stderr_data
        self._exit_code = exit_code
        self._never_exits = never_exits
        self.closed = False

    def recv_ready(self):
        return bool(self._out)

    def recv(self, n):
        chunk, self._out = self._out[:n], self._out[n:]
        return chunk

    def recv_stderr_ready(self):
        return bool(self._err)

    def recv_stderr(self, n):
        chunk, self._err = self._err[:n], self._err[n:]
        return chunk

    def exit_status_ready(self):
        if self._never_exits:
            return False
        return not self._out and not self._err

    def recv_exit_status(self):
        if self._out or self._err:
            raise AssertionError(
                "recv_exit_status() called with output undrained — "
                "window-full deadlock (hangs forever on real paramiko)"
            )
        return self._exit_code

    def close(self):
        self.closed = True


def _wire_exec_channel(mock_ssh, channel):
    """Point mock_ssh.exec_command at a (stdin, stdout, stderr) triple
    whose stdout.channel is the given mock channel."""
    stdout = MagicMock()
    stdout.channel = channel
    mock_ssh.exec_command.return_value = (MagicMock(), stdout, MagicMock())


class TestExecDrainLoop:
    """_exec must drain the channel and bound every wait.

    Regression tests for docs/issues/remote_backend_indefinite_wait_deadlock.md
    (job 2dbe6854: grep output 2,319,835 B > 2 MiB window wedged a job 8 h).
    """

    def test_large_output_does_not_deadlock(self, remote_backend):
        """Output bigger than the 2 MiB channel window must be returned,
        not deadlock. Fails on pre-fix code (recv_exit_status first)."""
        backend, mock_ssh, _ = remote_backend
        backend.connect()
        big = b"x" * (3 * 1024 * 1024)  # > 2 MiB window
        _wire_exec_channel(mock_ssh, _WindowedChannel(stdout_data=big))
        out = backend._exec("grep -rni role /ws")
        assert len(out) == 3 * 1024 * 1024

    def test_stderr_is_drained(self, remote_backend):
        """stderr shares the channel window; undrained stderr must not
        stall the loop even on a non-zero exit."""
        backend, mock_ssh, _ = remote_backend
        backend.connect()
        chan = _WindowedChannel(
            stdout_data=b"ok", stderr_data=b"e" * 100_000, exit_code=1
        )
        _wire_exec_channel(mock_ssh, chan)
        assert backend._exec("cmd") == "ok"

    def test_timeout_raises_and_closes_channel(self, remote_backend):
        """A command that never exits must raise RemoteCommandTimeoutError
        (NOT WorkspaceUnavailableError) and close the channel."""
        backend, mock_ssh, _ = remote_backend
        backend.connect()
        chan = _WindowedChannel(never_exits=True)
        _wire_exec_channel(mock_ssh, chan)
        with patch("time.sleep"):
            with pytest.raises(RemoteCommandTimeoutError):
                backend._exec("sleep 999", timeout=0)
        assert chan.closed
        assert not issubclass(RemoteCommandTimeoutError, WorkspaceUnavailableError)

    def test_output_truncated_at_cap(self, remote_backend):
        """Output beyond 5 MiB is dropped (marker appended), but the
        channel is still drained so the command can finish."""
        backend, mock_ssh, _ = remote_backend
        backend.connect()
        big = b"y" * (6 * 1024 * 1024)
        _wire_exec_channel(mock_ssh, _WindowedChannel(stdout_data=big))
        out = backend._exec("cat huge")
        assert out.endswith("[output truncated at 5 MiB]")
        assert len(out) < 6 * 1024 * 1024


# =============================================================================
# RemoteBackend Tests
# =============================================================================

# paramiko is installed in this environment, so RemoteBackend imports normally.
# We mock at the paramiko SSHClient/SFTPClient level rather than patching
# the module import.

from src.core.backends.remote import RemoteBackend, _RemoteTab  # noqa: E402


@pytest.fixture
def remote_backend():
    """Create a RemoteBackend instance with mocked SSH/SFTP.

    Patches paramiko.SSHClient so that connect() uses our mock objects.
    """
    mock_ssh = MagicMock()
    mock_sftp = MagicMock()
    mock_transport = MagicMock()
    mock_transport.is_active.return_value = True
    mock_ssh.get_transport.return_value = mock_transport
    mock_ssh.open_sftp.return_value = mock_sftp

    with patch("paramiko.SSHClient", return_value=mock_ssh):
        backend = RemoteBackend(
            host="10.0.0.42",
            port=22,
            username="agent-host",
            workspace_path="/home/agent-host/workspace",
            job_id="aaaa-bbbb-cccc-dddd",
            max_retries=2,
            connect_timeout=5,
        )
        yield backend, mock_ssh, mock_sftp


class TestRemoteBackendInit:
    """Tests for RemoteBackend initialization."""

    def test_init_stores_parameters(self):
        backend = RemoteBackend(
            host="192.168.1.100",
            port=2222,
            username="testuser",
            workspace_path="/opt/workspace/",
            job_id="test-job-123",
            scrollback_limit=3000,
            default_timeout=60,
            max_tabs=10,
        )
        assert backend._host == "192.168.1.100"
        assert backend._port == 2222
        assert backend._username == "testuser"
        assert backend._remote_root == "/opt/workspace"  # trailing slash stripped
        assert backend._job_id == "test-job-123"
        assert backend._scrollback_limit == 3000
        assert backend._default_timeout == 60
        assert backend._max_tabs == 10

    def test_init_default_blocked_commands(self):
        backend = RemoteBackend(host="host", workspace_path="/ws")
        assert "reboot" in backend._blocked_commands
        assert "shutdown" in backend._blocked_commands
        assert "poweroff" in backend._blocked_commands

    def test_init_custom_blocked_commands(self):
        backend = RemoteBackend(
            host="host", workspace_path="/ws", blocked_commands=["rm", "dd"]
        )
        assert backend._blocked_commands == frozenset(["rm", "dd"])

    def test_init_without_paramiko_raises(self):
        """RemoteBackend.__init__ raises when paramiko is None at module level."""
        import src.core.backends.remote as remote_mod

        original = remote_mod.paramiko
        try:
            remote_mod.paramiko = None
            with pytest.raises(ImportError, match="paramiko is required"):
                RemoteBackend(host="host", workspace_path="/ws")
        finally:
            remote_mod.paramiko = original

    def test_root_property(self):
        backend = RemoteBackend(
            host="host", workspace_path="/home/agent-host/workspace"
        )
        assert backend.root == "/home/agent-host/workspace"

    def test_supports_shell(self):
        backend = RemoteBackend(host="host", workspace_path="/ws")
        assert backend.supports_shell is True

    def test_session_name_from_job_id(self):
        backend = RemoteBackend(
            host="host", workspace_path="/ws", job_id="abcdef123456-rest"
        )
        assert backend._session_name == "agent_abcdef123456"

    def test_session_name_without_job_id(self):
        backend = RemoteBackend(host="host", workspace_path="/ws", job_id="")
        assert backend._session_name == "agent_remote"


class TestRemoteBackendConnect:
    """Tests for RemoteBackend.connect()."""

    def test_connect_success(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        assert backend._ssh is not None
        assert backend._sftp is not None
        mock_ssh.set_missing_host_key_policy.assert_called_once()
        mock_ssh.connect.assert_called_once_with(
            hostname="10.0.0.42",
            port=22,
            username="agent-host",
            timeout=5,
        )

    def test_connect_with_key_path(self):
        mock_ssh = MagicMock()
        mock_sftp = MagicMock()
        mock_ssh.open_sftp.return_value = mock_sftp

        with patch("paramiko.SSHClient", return_value=mock_ssh):
            backend = RemoteBackend(
                host="10.0.0.42",
                workspace_path="/ws",
                key_path="/home/user/.ssh/id_rsa",
                max_retries=1,
            )
            backend.connect()
        connect_call = mock_ssh.connect.call_args
        assert connect_call.kwargs.get("key_filename") == "/home/user/.ssh/id_rsa"

    def test_connect_retries_on_failure(self, remote_backend):
        """connect() retries with exponential backoff on SSH errors."""
        import paramiko as real_paramiko

        backend, mock_ssh, mock_sftp = remote_backend

        # First call fails, second succeeds
        mock_ssh.connect.side_effect = [
            real_paramiko.SSHException("connection refused"),
            None,
        ]

        with patch("time.sleep"):
            backend.connect()

        assert mock_ssh.connect.call_count == 2

    def test_connect_raises_after_max_retries(self, remote_backend):
        """connect() raises WorkspaceUnavailableError after all retries exhausted."""
        backend, mock_ssh, mock_sftp = remote_backend

        mock_ssh.connect.side_effect = socket.error("no route to host")

        with patch("time.sleep"):
            with pytest.raises(WorkspaceUnavailableError, match="Failed to connect"):
                backend.connect()

        # max_retries = 2, so 2 attempts
        assert mock_ssh.connect.call_count == 2


class TestRemoteBackendConnectClassification:
    """connect() classifies the failure cause and sizes the retry budget."""

    @contextmanager
    def _scenario(self, side_effect, retries):
        """Yield (backend, mock_ssh) with paramiko.SSHClient AND time.sleep
        patched for the whole scope — connect() must run while paramiko is
        mocked, or it dials a real host (5s network timeout)."""
        mock_ssh = MagicMock()
        mock_ssh.open_sftp.return_value = MagicMock()
        mock_ssh.connect.side_effect = side_effect
        with patch("paramiko.SSHClient", return_value=mock_ssh), patch("time.sleep"):
            backend = RemoteBackend(
                host="10.0.0.42",
                workspace_path="/ws",
                connect_timeout=5,
                max_retries=retries,
            )
            yield backend, mock_ssh

    def test_gone_dns_fails_fast_no_retry(self):
        """gaierror (NXDOMAIN) = pod gone → raise on the first attempt."""
        err = socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        with self._scenario(err, 5) as (backend, mock_ssh):
            with pytest.raises(WorkspaceUnavailableError):
                backend.connect()
            assert mock_ssh.connect.call_count == 1

    def test_no_route_fails_fast(self):
        """OSError EHOSTUNREACH = no route → gone → fail fast."""
        err = OSError(errno.EHOSTUNREACH, "No route to host")
        with self._scenario(err, 5) as (backend, mock_ssh):
            with pytest.raises(WorkspaceUnavailableError):
                backend.connect()
            assert mock_ssh.connect.call_count == 1

    def test_connection_refused_uses_full_boot_budget(self):
        """ECONNREFUSED = sshd booting → keep the full max_retries budget."""
        err = ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")
        with self._scenario(err, 5) as (backend, mock_ssh):
            with pytest.raises(WorkspaceUnavailableError):
                backend.connect()
            assert mock_ssh.connect.call_count == 5

    def test_timeout_capped_at_two(self):
        """Ambiguous (timeout) → short cap, not the full budget."""
        with self._scenario(socket.timeout("timed out"), 5) as (backend, mock_ssh):
            with pytest.raises(WorkspaceUnavailableError):
                backend.connect()
            assert mock_ssh.connect.call_count == 2

    def test_vm_timeout_can_use_full_boot_budget(self):
        """VM-over-tailnet timeout can represent boot/peer convergence."""
        err = socket.timeout("timed out")
        with self._scenario(err, 5) as (backend, mock_ssh):
            backend._retry_timeouts_as_booting = True
            with pytest.raises(WorkspaceUnavailableError):
                backend.connect()
            assert mock_ssh.connect.call_count == 5

    def test_vm_timeout_budget_is_first_connect_only(self):
        """After one successful SSH session, VM timeouts use the short cap."""
        mock_ssh = MagicMock()
        mock_ssh.open_sftp.return_value = MagicMock()
        with patch("paramiko.SSHClient", return_value=mock_ssh), patch("time.sleep"):
            backend = RemoteBackend(
                host="10.0.0.42",
                workspace_path="/ws",
                connect_timeout=5,
                max_retries=5,
                retry_timeouts_as_booting=True,
            )
            backend.connect()
            mock_ssh.connect.reset_mock()
            mock_ssh.connect.side_effect = socket.timeout("timed out")

            with pytest.raises(WorkspaceUnavailableError):
                backend.connect()

            assert mock_ssh.connect.call_count == 2

    def test_message_says_workspace_not_vm(self):
        """Renamed error string: 'workspace', never 'VM'."""
        err = socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        with self._scenario(err, 1) as (backend, mock_ssh):
            with pytest.raises(WorkspaceUnavailableError) as exc:
                backend.connect()
        assert "workspace" in str(exc.value)
        assert "VM" not in str(exc.value)


class TestRemoteBackendDisconnect:
    """Tests for RemoteBackend.disconnect()."""

    def test_disconnect_closes_resources(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        backend.disconnect()
        mock_sftp.close.assert_called_once()
        mock_ssh.close.assert_called_once()
        assert backend._ssh is None
        assert backend._sftp is None

    def test_disconnect_kills_tmux_session(self, remote_backend):
        """disconnect() kills the remote tmux session if shell was initialized."""
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        backend._shell_initialized = True
        backend._tabs["default"] = MagicMock()

        # Mock exec_command for the tmux kill
        _wire_exec_channel(mock_ssh, _WindowedChannel())

        backend.disconnect()
        assert backend._shell_initialized is False
        assert len(backend._tabs) == 0

    def test_disconnect_without_connect_is_safe(self, remote_backend):
        """disconnect() on a never-connected backend should not raise."""
        backend, mock_ssh, mock_sftp = remote_backend
        backend.disconnect()  # Should not raise

    def test_disconnect_handles_sftp_close_error(self, remote_backend):
        """disconnect() handles errors during SFTP close gracefully."""
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        mock_sftp.close.side_effect = Exception("already closed")
        backend.disconnect()  # Should not raise
        assert backend._sftp is None


class TestRemoteBackendIsConnected:
    """Tests for RemoteBackend.is_connected()."""

    def test_connected_after_connect(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        assert backend.is_connected() is True

    def test_not_connected_initially(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        assert backend.is_connected() is False

    def test_not_connected_when_transport_inactive(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        # Simulate transport becoming inactive
        transport = mock_ssh.get_transport.return_value
        transport.is_active.return_value = False
        assert backend.is_connected() is False

    def test_not_connected_when_transport_none(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        mock_ssh.get_transport.return_value = None
        assert backend.is_connected() is False


class TestRemoteBackendEnsureConnected:
    """Tests for RemoteBackend._ensure_connected()."""

    def test_noop_when_connected(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        # Reset call count
        mock_ssh.connect.reset_mock()
        backend._ensure_connected()
        # Should not attempt reconnection
        mock_ssh.connect.assert_not_called()

    def test_reconnects_when_disconnected(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        # Simulate connection drop
        transport = mock_ssh.get_transport.return_value
        transport.is_active.return_value = False

        # After reconnect, transport is active again
        def restore_active(*args, **kwargs):
            transport.is_active.return_value = True

        mock_ssh.connect.side_effect = restore_active

        with patch("time.sleep"):
            backend._ensure_connected()

    def test_raises_after_reconnect_failure(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        # Simulate connection drop
        transport = mock_ssh.get_transport.return_value
        transport.is_active.return_value = False

        # All reconnect attempts fail
        mock_ssh.connect.side_effect = socket.error("unreachable")

        with patch("time.sleep"):
            with pytest.raises(WorkspaceUnavailableError):
                backend._ensure_connected()

    def test_ensure_connected_does_not_multiply_retry_budget(self, remote_backend):
        """_ensure_connected must not wrap connect()'s own retry loop in a second
        loop — a dead host should cost max_retries attempts, not max_retries²."""
        backend, mock_ssh, mock_sftp = remote_backend  # fixture: max_retries=2
        backend._ssh = None  # force is_connected() False → reconnect path
        mock_ssh.connect.side_effect = socket.error("host down")

        with patch("time.sleep"):
            with pytest.raises(WorkspaceUnavailableError):
                backend._ensure_connected()

        # max_retries=2 → 2 attempts. The nested bug produced 2*2 = 4.
        assert mock_ssh.connect.call_count == 2


class TestRemoteBackendPathResolution:
    """Tests for RemoteBackend._resolve() and resolve_path()."""

    def test_resolve_empty_returns_root(self, remote_backend):
        backend, _, _ = remote_backend
        assert backend._resolve("") == "/home/agent-host/workspace"

    def test_resolve_dot_returns_root(self, remote_backend):
        backend, _, _ = remote_backend
        assert backend._resolve(".") == "/home/agent-host/workspace"

    def test_resolve_simple_path(self, remote_backend):
        backend, _, _ = remote_backend
        result = backend._resolve("docs/file.txt")
        assert result == "/home/agent-host/workspace/docs/file.txt"

    def test_resolve_rejects_parent_traversal(self, remote_backend):
        backend, _, _ = remote_backend
        with pytest.raises(ValueError, match="escapes workspace boundary"):
            backend._resolve("../../etc/passwd")

    def test_resolve_rejects_absolute_path(self, remote_backend):
        backend, _, _ = remote_backend
        with pytest.raises(ValueError, match="escapes workspace boundary"):
            backend._resolve("/etc/passwd")

    def test_resolve_normalizes_dots(self, remote_backend):
        backend, _, _ = remote_backend
        result = backend._resolve("a/./b/../c")
        assert result == "/home/agent-host/workspace/a/c"

    def test_resolve_path_method(self, remote_backend):
        backend, _, _ = remote_backend
        assert backend.resolve_path("test.txt") == "/home/agent-host/workspace/test.txt"


class TestRemoteBackendReadFile:
    """Tests for RemoteBackend.read_file()."""

    def _setup_connected(self, backend, mock_ssh, mock_sftp):
        """Connect the backend and set up transport as active."""
        backend.connect()

    def test_read_text_file(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        self._setup_connected(backend, mock_ssh, mock_sftp)

        file_data = b"Hello, remote world!"
        file_obj = io.BytesIO(file_data)
        mock_sftp.open.return_value.__enter__ = MagicMock(return_value=file_obj)
        mock_sftp.open.return_value.__exit__ = MagicMock(return_value=False)

        content = backend.read_file("hello.txt")
        assert content == "Hello, remote world!"
        mock_sftp.open.assert_called_with("/home/agent-host/workspace/hello.txt", "rb")

    def test_read_binary_file(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        self._setup_connected(backend, mock_ssh, mock_sftp)

        file_data = b"\x89PNG\r\n"
        file_obj = io.BytesIO(file_data)
        mock_sftp.open.return_value.__enter__ = MagicMock(return_value=file_obj)
        mock_sftp.open.return_value.__exit__ = MagicMock(return_value=False)

        content = backend.read_file("image.png", binary=True)
        assert content == file_data
        assert isinstance(content, bytes)

    def test_read_nonexistent_raises(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        self._setup_connected(backend, mock_ssh, mock_sftp)

        mock_sftp.open.side_effect = FileNotFoundError("no such file")
        with pytest.raises(FileNotFoundError, match="File not found"):
            backend.read_file("ghost.txt")

    def test_read_ioerror_raises_file_not_found(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        self._setup_connected(backend, mock_ssh, mock_sftp)

        mock_sftp.open.side_effect = IOError("permission denied")
        with pytest.raises(FileNotFoundError, match="Cannot read"):
            backend.read_file("restricted.txt")

    def test_read_path_traversal_rejected(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        self._setup_connected(backend, mock_ssh, mock_sftp)

        with pytest.raises(ValueError, match="escapes workspace boundary"):
            backend.read_file("../../etc/shadow")


class TestRemoteBackendWriteFile:
    """Tests for RemoteBackend.write_file()."""

    def test_write_text_file(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        # Mock the parent directory check
        mock_sftp.stat.return_value = _make_sftp_attr(is_dir=True)

        file_obj = MagicMock()
        mock_sftp.open.return_value.__enter__ = MagicMock(return_value=file_obj)
        mock_sftp.open.return_value.__exit__ = MagicMock(return_value=False)

        backend.write_file("output.txt", "Hello!")
        mock_sftp.open.assert_called_with("/home/agent-host/workspace/output.txt", "wb")

    def test_write_binary_content(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        mock_sftp.stat.return_value = _make_sftp_attr(is_dir=True)
        file_obj = MagicMock()
        mock_sftp.open.return_value.__enter__ = MagicMock(return_value=file_obj)
        mock_sftp.open.return_value.__exit__ = MagicMock(return_value=False)

        data = b"\x00\x01\x02"
        backend.write_file("data.bin", data)
        file_obj.write.assert_called_once_with(data)

    def test_write_text_encoded_as_utf8(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        mock_sftp.stat.return_value = _make_sftp_attr(is_dir=True)
        file_obj = MagicMock()
        mock_sftp.open.return_value.__enter__ = MagicMock(return_value=file_obj)
        mock_sftp.open.return_value.__exit__ = MagicMock(return_value=False)

        backend.write_file("text.txt", "Hello")
        file_obj.write.assert_called_once_with(b"Hello")


class TestRemoteBackendHomeFile:
    """Tests for RemoteBackend.write_home_file() and resolve_home_path()."""

    def _setup(self, remote_backend, *, home: str = "/home/agent-host"):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        mock_sftp.normalize.return_value = home
        # Make _ensure_remote_dir's stat probe see existing parents so the
        # mkdir loop terminates immediately.
        mock_sftp.stat.return_value = _make_sftp_attr(is_dir=True)
        return backend, mock_ssh, mock_sftp

    def test_resolve_home_path_joins_home(self, remote_backend):
        backend, _, mock_sftp = self._setup(remote_backend)
        assert backend.resolve_home_path(".ssh/repo_x") == (
            "/home/agent-host/.ssh/repo_x"
        )
        # Cached after first call: normalize called exactly once.
        backend.resolve_home_path(".ssh/repo_y")
        mock_sftp.normalize.assert_called_once_with(".")

    def test_resolve_home_path_normalizes(self, remote_backend):
        backend, _, _ = self._setup(remote_backend)
        assert (
            backend.resolve_home_path(".ssh/./nested/../repo")
            == "/home/agent-host/.ssh/repo"
        )

    def test_resolve_home_path_rejects_empty(self, remote_backend):
        backend, _, _ = self._setup(remote_backend)
        with pytest.raises(ValueError, match="non-empty"):
            backend.resolve_home_path("")
        with pytest.raises(ValueError, match="non-empty"):
            backend.resolve_home_path(".")

    def test_resolve_home_path_rejects_absolute(self, remote_backend):
        backend, _, _ = self._setup(remote_backend)
        with pytest.raises(ValueError, match="escapes home directory"):
            backend.resolve_home_path("/etc/passwd")

    def test_resolve_home_path_rejects_traversal(self, remote_backend):
        backend, _, _ = self._setup(remote_backend)
        with pytest.raises(ValueError, match="escapes home directory"):
            backend.resolve_home_path("../../etc/passwd")

    def test_write_home_file_writes_under_home(self, remote_backend):
        backend, _, mock_sftp = self._setup(remote_backend)
        file_obj = MagicMock()
        mock_sftp.open.return_value.__enter__ = MagicMock(return_value=file_obj)
        mock_sftp.open.return_value.__exit__ = MagicMock(return_value=False)

        backend.write_home_file(".ssh/repo_foo", "PRIVATE KEY")
        mock_sftp.open.assert_called_with("/home/agent-host/.ssh/repo_foo", "wb")
        file_obj.write.assert_called_once_with(b"PRIVATE KEY")

    def test_write_home_file_accepts_bytes(self, remote_backend):
        backend, _, mock_sftp = self._setup(remote_backend)
        file_obj = MagicMock()
        mock_sftp.open.return_value.__enter__ = MagicMock(return_value=file_obj)
        mock_sftp.open.return_value.__exit__ = MagicMock(return_value=False)

        backend.write_home_file(".ssh/repo_bin", b"\x00\xff")
        file_obj.write.assert_called_once_with(b"\x00\xff")

    def test_write_home_file_rejects_escape(self, remote_backend):
        backend, _, _ = self._setup(remote_backend)
        with pytest.raises(ValueError, match="escapes home directory"):
            backend.write_home_file("/etc/passwd", "x")
        with pytest.raises(ValueError, match="escapes home directory"):
            backend.write_home_file("../escape", "x")


class TestRemoteBackendAppendFile:
    """Tests for RemoteBackend.append_file()."""

    def test_append_opens_in_append_mode(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        mock_sftp.stat.return_value = _make_sftp_attr(is_dir=True)
        file_obj = MagicMock()
        mock_sftp.open.return_value.__enter__ = MagicMock(return_value=file_obj)
        mock_sftp.open.return_value.__exit__ = MagicMock(return_value=False)

        backend.append_file("log.txt", "new line\n")
        mock_sftp.open.assert_called_with("/home/agent-host/workspace/log.txt", "ab")
        file_obj.write.assert_called_once_with(b"new line\n")


class TestRemoteBackendExists:
    """Tests for RemoteBackend.exists(), is_file(), is_dir()."""

    def test_exists_true(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        mock_sftp.stat.return_value = _make_sftp_attr()
        assert backend.exists("file.txt") is True

    def test_exists_false(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        mock_sftp.stat.side_effect = FileNotFoundError()
        assert backend.exists("ghost.txt") is False

    def test_is_file_true(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        mock_sftp.stat.return_value = _make_sftp_attr(is_dir=False)
        assert backend.is_file("file.txt") is True

    def test_is_file_false_for_dir(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        mock_sftp.stat.return_value = _make_sftp_attr(is_dir=True)
        assert backend.is_file("dir") is False

    def test_is_file_false_for_nonexistent(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        mock_sftp.stat.side_effect = FileNotFoundError()
        assert backend.is_file("ghost.txt") is False

    def test_is_dir_true(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        mock_sftp.stat.return_value = _make_sftp_attr(is_dir=True)
        assert backend.is_dir("subdir") is True

    def test_is_dir_false_for_file(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        mock_sftp.stat.return_value = _make_sftp_attr(is_dir=False)
        assert backend.is_dir("file.txt") is False

    def test_is_dir_false_for_nonexistent(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        mock_sftp.stat.side_effect = FileNotFoundError()
        assert backend.is_dir("ghost") is False


class TestRemoteBackendListDir:
    """Tests for RemoteBackend.list_dir()."""

    def test_list_dir_with_entries(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        # stat returns directory
        mock_sftp.stat.return_value = _make_sftp_attr(is_dir=True)

        entries = [
            _make_sftp_entry("file.txt", is_dir=False),
            _make_sftp_entry("subdir", is_dir=True),
        ]
        mock_sftp.listdir_attr.return_value = entries

        result = backend.list_dir("")
        assert "file.txt" in result
        assert "subdir/" in result

    def test_list_dir_nonexistent_returns_empty(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        mock_sftp.stat.side_effect = FileNotFoundError()
        result = backend.list_dir("ghost_dir")
        assert result == []

    def test_list_dir_not_a_directory(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        mock_sftp.stat.return_value = _make_sftp_attr(is_dir=False)
        result = backend.list_dir("file.txt")
        assert result == ["file.txt"]

    def test_list_dir_with_pattern(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        mock_sftp.stat.return_value = _make_sftp_attr(is_dir=True)
        entries = [
            _make_sftp_entry("a.py", is_dir=False),
            _make_sftp_entry("b.py", is_dir=False),
            _make_sftp_entry("c.txt", is_dir=False),
        ]
        mock_sftp.listdir_attr.return_value = entries

        result = backend.list_dir("", pattern="*.py")
        assert "a.py" in result
        assert "b.py" in result
        assert "c.txt" not in result

    def test_list_dir_sorted(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        mock_sftp.stat.return_value = _make_sftp_attr(is_dir=True)
        entries = [
            _make_sftp_entry("z.txt", is_dir=False),
            _make_sftp_entry("a.txt", is_dir=False),
            _make_sftp_entry("m.txt", is_dir=False),
        ]
        mock_sftp.listdir_attr.return_value = entries

        result = backend.list_dir("")
        assert result == sorted(result)

    def test_list_dir_ioerror_returns_empty(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        mock_sftp.stat.return_value = _make_sftp_attr(is_dir=True)
        mock_sftp.listdir_attr.side_effect = IOError("permission denied")

        result = backend.list_dir("")
        assert result == []


class TestRemoteBackendSearchFiles:
    """Tests for RemoteBackend.search_files() (server-side grep)."""

    def _setup_exec(self, mock_ssh, output: str, exit_code: int = 0):
        """Set up mock exec_command to return given output."""
        _wire_exec_channel(
            mock_ssh,
            _WindowedChannel(stdout_data=output.encode("utf-8"), exit_code=exit_code),
        )

    def test_search_parses_grep_output(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        grep_output = (
            "/home/agent-host/workspace/file.txt:5:found the target here\n"
            "/home/agent-host/workspace/sub/other.py:12:another target line\n"
        )
        self._setup_exec(mock_ssh, grep_output)

        results = backend.search_files("target")
        assert len(results) == 2
        assert results[0]["path"] == "file.txt"
        assert results[0]["line_number"] == 5
        assert "target" in results[0]["line"]
        assert results[1]["path"] == "sub/other.py"
        assert results[1]["line_number"] == 12

    def test_search_no_results(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        self._setup_exec(mock_ssh, "")
        results = backend.search_files("nonexistent")
        assert results == []

    def test_search_case_insensitive_flag(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        self._setup_exec(mock_ssh, "")

        backend.search_files("Test", case_sensitive=False)
        cmd = mock_ssh.exec_command.call_args[0][0]
        assert "-rni" in cmd

    def test_search_case_sensitive_flag(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        self._setup_exec(mock_ssh, "")

        backend.search_files("Test", case_sensitive=True)
        cmd = mock_ssh.exec_command.call_args[0][0]
        assert "-rn " in cmd  # no 'i' flag
        assert "-rni" not in cmd

    def test_search_escapes_single_quotes(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        self._setup_exec(mock_ssh, "")

        backend.search_files("it's a test")
        cmd = mock_ssh.exec_command.call_args[0][0]
        assert "'\\''" in cmd  # escaped single quote

    def test_search_skips_malformed_lines(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        grep_output = (
            "malformed line without colons\n"
            "/home/agent-host/workspace/good.txt:1:valid result\n"
            "\n"
        )
        self._setup_exec(mock_ssh, grep_output)

        results = backend.search_files("query")
        assert len(results) == 1
        assert results[0]["path"] == "good.txt"

    def test_search_handles_invalid_line_number(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        grep_output = "/home/agent-host/workspace/file.txt:notanum:some content\n"
        self._setup_exec(mock_ssh, grep_output)

        results = backend.search_files("query")
        assert results == []


class TestSearchFilesCap:
    """search_files must bound grep output server-side: the display cap
    is 50 matches, yet uncapped grep shipped 2.2 MB in the incident."""

    def test_grep_command_is_head_capped(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        with patch.object(backend, "_exec", return_value="") as ex:
            backend.search_files("role")
        cmd = ex.call_args.args[0]
        assert "| head -n 2000" in cmd
        assert cmd.rstrip().endswith("|| true")


class TestRemoteBackendMkdir:
    """Tests for RemoteBackend.mkdir()."""

    def test_mkdir_calls_ensure_remote_dir(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        # First call to stat returns None (doesn't exist), second returns dir
        call_count = [0]
        root_attr = _make_sftp_attr(is_dir=True)

        def stat_side_effect(path):
            call_count[0] += 1
            if path == "/home/agent-host/workspace/new_dir":
                raise FileNotFoundError()
            return root_attr

        mock_sftp.stat.side_effect = stat_side_effect
        backend.mkdir("new_dir")
        mock_sftp.mkdir.assert_called()


class TestRemoteBackendDeleteFile:
    """Tests for RemoteBackend.delete_file()."""

    def test_delete_existing_file(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        mock_sftp.stat.return_value = _make_sftp_attr(is_dir=False)
        result = backend.delete_file("target.txt")
        assert result is True
        mock_sftp.remove.assert_called_once_with(
            "/home/agent-host/workspace/target.txt"
        )

    def test_delete_nonexistent_returns_false(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        mock_sftp.stat.side_effect = FileNotFoundError()
        result = backend.delete_file("ghost.txt")
        assert result is False

    def test_delete_empty_directory(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        mock_sftp.stat.return_value = _make_sftp_attr(is_dir=True)
        mock_sftp.listdir.return_value = []
        result = backend.delete_file("empty_dir")
        assert result is True
        mock_sftp.rmdir.assert_called_once()

    def test_delete_nonempty_directory_raises(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        mock_sftp.stat.return_value = _make_sftp_attr(is_dir=True)
        mock_sftp.listdir.return_value = ["child.txt"]
        with pytest.raises(ValueError, match="Cannot delete non-empty directory"):
            backend.delete_file("nonempty_dir")


class TestRemoteBackendDeleteDirectory:
    """Tests for RemoteBackend.delete_directory()."""

    def test_delete_directory_uses_rm_rf(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        mock_sftp.stat.return_value = _make_sftp_attr(is_dir=True)

        # Mock exec for rm -rf
        _wire_exec_channel(mock_ssh, _WindowedChannel())

        result = backend.delete_directory("tree")
        assert result is True
        cmd = mock_ssh.exec_command.call_args[0][0]
        assert "rm -rf" in cmd

    def test_delete_root_raises(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        with pytest.raises(ValueError, match="Cannot delete workspace root"):
            backend.delete_directory("")

    def test_delete_nonexistent_returns_false(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        mock_sftp.stat.side_effect = FileNotFoundError()
        result = backend.delete_directory("ghost_dir")
        assert result is False

    def test_delete_file_as_directory_raises(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        mock_sftp.stat.return_value = _make_sftp_attr(is_dir=False)
        with pytest.raises(ValueError, match="Not a directory"):
            backend.delete_directory("file.txt")


class TestRemoteBackendMove:
    """Tests for RemoteBackend.move()."""

    def _setup_exec(self, mock_ssh):
        _wire_exec_channel(mock_ssh, _WindowedChannel())

    def test_move_calls_mv(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        mock_sftp.stat.return_value = _make_sftp_attr()
        self._setup_exec(mock_ssh)

        backend.move("old.txt", "new.txt")
        cmd = mock_ssh.exec_command.call_args[0][0]
        assert "mv" in cmd
        assert "/home/agent-host/workspace/old.txt" in cmd
        assert "/home/agent-host/workspace/new.txt" in cmd

    def test_move_nonexistent_raises(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        mock_sftp.stat.side_effect = FileNotFoundError()
        with pytest.raises(FileNotFoundError, match="Source not found"):
            backend.move("ghost.txt", "dst.txt")


class TestRemoteBackendCopy:
    """Tests for RemoteBackend.copy()."""

    def _setup_exec(self, mock_ssh):
        _wire_exec_channel(mock_ssh, _WindowedChannel())

    def test_copy_calls_cp(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        mock_sftp.stat.return_value = _make_sftp_attr(is_dir=False)
        self._setup_exec(mock_ssh)

        backend.copy("src.txt", "dst.txt")
        cmd = mock_ssh.exec_command.call_args[0][0]
        assert "cp -a" in cmd

    def test_copy_nonexistent_raises(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        mock_sftp.stat.side_effect = FileNotFoundError()
        with pytest.raises(FileNotFoundError, match="Source not found"):
            backend.copy("ghost.txt", "dst.txt")

    def test_copy_directory_raises(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        mock_sftp.stat.return_value = _make_sftp_attr(is_dir=True)
        with pytest.raises(ValueError, match="Cannot copy directory"):
            backend.copy("dir", "dst")


class TestRemoteBackendStat:
    """Tests for RemoteBackend.stat()."""

    def test_stat_file_returns_size(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        mock_sftp.stat.return_value = _make_sftp_attr(is_dir=False, size=1024)
        size = backend.stat("file.txt")
        assert size == 1024

    def test_stat_nonexistent_returns_zero(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        mock_sftp.stat.side_effect = FileNotFoundError()
        assert backend.stat("ghost.txt") == 0

    def test_stat_directory_uses_du(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        mock_sftp.stat.return_value = _make_sftp_attr(is_dir=True)

        _wire_exec_channel(
            mock_ssh,
            _WindowedChannel(stdout_data=b"4096\t/home/agent-host/workspace/dir\n"),
        )

        size = backend.stat("dir")
        assert size == 4096

    def test_stat_directory_du_parse_error(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        mock_sftp.stat.return_value = _make_sftp_attr(is_dir=True)

        _wire_exec_channel(
            mock_ssh, _WindowedChannel(stdout_data=b"malformed output\n")
        )

        assert backend.stat("dir") == 0


class TestHeavyOpTimeouts:
    """Timeouts now actually bind (Task 1), so heavy ops need explicit
    generous deadlines or big trees would newly fail at the 30s default."""

    @pytest.mark.parametrize(
        "call,expected_timeout",
        [
            (lambda b: b.delete_directory("big"), 300),
            (lambda b: b.copy("a", "b"), 300),
            (lambda b: b.move("a", "b"), 120),
            (lambda b: b.stat("big"), 120),
        ],
    )
    def test_heavy_ops_pass_generous_timeouts(
        self, remote_backend, call, expected_timeout
    ):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        # Mock stat to return directory for "big" paths, files for others
        def mock_stat(path):
            if "big" in path:
                return _make_sftp_attr(is_dir=True)
            else:
                return _make_sftp_attr(is_dir=False)

        mock_sftp.stat.side_effect = mock_stat
        with patch.object(backend, "_exec", return_value="0\t/ws") as ex:
            call(backend)
        assert ex.call_args.kwargs.get("timeout") == expected_timeout


class TestRemoteBackendExec:
    """Tests for RemoteBackend._exec()."""

    def test_exec_returns_stdout(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        _wire_exec_channel(mock_ssh, _WindowedChannel(stdout_data=b"output data\n"))

        result = backend._exec("echo hello")
        assert result == "output data\n"

    def test_exec_nonzero_exit_still_returns_output(self, remote_backend):
        """Non-zero exit codes are logged but output is still returned."""
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        _wire_exec_channel(
            mock_ssh,
            _WindowedChannel(
                stdout_data=b"some output\n", stderr_data=b"error msg", exit_code=1
            ),
        )

        result = backend._exec("grep notfound .")
        assert result == "some output\n"

    def test_exec_ssh_error_raises(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        mock_ssh.exec_command.side_effect = socket.error("broken pipe")
        with pytest.raises(WorkspaceUnavailableError, match="SSH command failed"):
            backend._exec("ls")


class TestRemoteBackendCheckBlocked:
    """Tests for RemoteBackend._check_blocked()."""

    def test_blocked_command_detected(self, remote_backend):
        backend, _, _ = remote_backend
        result = backend._check_blocked("reboot -f")
        assert result is not None
        assert "reboot" in result

    def test_allowed_command_returns_none(self, remote_backend):
        backend, _, _ = remote_backend
        result = backend._check_blocked("ls -la")
        assert result is None

    def test_empty_command_returns_none(self, remote_backend):
        backend, _, _ = remote_backend
        result = backend._check_blocked("")
        assert result is None

    def test_shutdown_blocked(self, remote_backend):
        backend, _, _ = remote_backend
        result = backend._check_blocked("shutdown now")
        assert result is not None

    def test_custom_blocked_commands(self):
        backend = RemoteBackend(
            host="host", workspace_path="/ws", blocked_commands=["rm"]
        )
        assert backend._check_blocked("rm -rf /") is not None
        assert backend._check_blocked("ls -la") is None
        assert backend._check_blocked("reboot") is None  # not in custom list

    def test_sudo_freeze_returns_sentinel(self):
        """sudo_action='freeze' (default) returns SUDO_FREEZE_SENTINEL."""
        from src.tools.shell.shell_manager import SUDO_FREEZE_SENTINEL

        backend = RemoteBackend(host="host", workspace_path="/ws", sudo_action="freeze")
        result = backend._check_blocked("sudo apt-get install -y libxml2-dev")
        assert result == SUDO_FREEZE_SENTINEL

    def test_sudo_allow_passes_through(self):
        """sudo_action='allow' returns None (VM-backed agents)."""
        backend = RemoteBackend(host="host", workspace_path="/ws", sudo_action="allow")
        result = backend._check_blocked("sudo apt-get install -y libxml2-dev")
        assert result is None

    def test_sudo_block_returns_error(self):
        """sudo_action='block' returns an error message."""
        backend = RemoteBackend(host="host", workspace_path="/ws", sudo_action="block")
        result = backend._check_blocked("sudo ls")
        assert result is not None
        assert "blocked" in result.lower()

    def test_sudo_freeze_is_default(self):
        """Default sudo_action is 'freeze'."""
        backend = RemoteBackend(host="host", workspace_path="/ws")
        assert backend._sudo_action == "freeze"


class TestRemoteBackendInteractiveDetection:
    """Tests for RemoteBackend._detect_interactive_prompt()."""

    def test_detects_yn_prompt(self, remote_backend):
        backend, _, _ = remote_backend
        lines = ["Some output", "Do you want to continue? [y/N]"]
        result = backend._detect_interactive_prompt(lines)
        assert result is not None
        assert "confirmation" in result

    def test_detects_password_prompt(self, remote_backend):
        backend, _, _ = remote_backend
        lines = ["Connecting...", "Password:"]
        result = backend._detect_interactive_prompt(lines)
        assert result is not None
        assert "password" in result

    def test_detects_sudo_prompt(self, remote_backend):
        backend, _, _ = remote_backend
        lines = ["[sudo] password for user:"]
        result = backend._detect_interactive_prompt(lines)
        assert result is not None

    def test_no_prompt_returns_none(self, remote_backend):
        backend, _, _ = remote_backend
        lines = ["total 42", "-rw-r--r-- 1 user user 100 file.txt", "user@host:~$"]
        result = backend._detect_interactive_prompt(lines)
        assert result is None

    def test_detects_ssh_host_key(self, remote_backend):
        backend, _, _ = remote_backend
        lines = ["Are you sure you want to continue connecting (yes/no)?"]
        result = backend._detect_interactive_prompt(lines)
        assert result is not None

    def test_only_checks_last_5_lines(self, remote_backend):
        """Prompt detection only checks the last 5 lines."""
        backend, _, _ = remote_backend
        lines = [
            "Password: (old prompt from scrollback)",
            "normal line 1",
            "normal line 2",
            "normal line 3",
            "normal line 4",
            "normal line 5",
        ]
        # The Password: is in position 0, but only last 5 are checked
        result = backend._detect_interactive_prompt(lines)
        assert result is None


class TestRemoteBackendDetectBlockedTab:
    """Tests for RemoteBackend._detect_blocked_tab()."""

    def test_normal_prompt_not_blocked(self, remote_backend):
        backend, _, _ = remote_backend
        lines = ["some output", "user@host:~$"]
        result = backend._detect_blocked_tab(lines)
        assert result is None

    def test_hash_prompt_not_blocked(self, remote_backend):
        backend, _, _ = remote_backend
        lines = ["output", "root@host:#"]
        result = backend._detect_blocked_tab(lines)
        assert result is None

    def test_interactive_prompt_blocked(self, remote_backend):
        backend, _, _ = remote_backend
        lines = ["Install packages?", "[y/N]"]
        result = backend._detect_blocked_tab(lines)
        assert result is not None


class TestRemoteBackendShellOperations:
    """Tests for RemoteBackend shell tab management."""

    def _setup_exec_mock(self, mock_ssh, output: str = "", exit_code: int = 0):
        _wire_exec_channel(
            mock_ssh,
            _WindowedChannel(stdout_data=output.encode("utf-8"), exit_code=exit_code),
        )

    def test_shell_list_tabs_after_init(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        self._setup_exec_mock(mock_ssh)

        with patch("time.sleep"):
            backend._init_shell()

        tabs = backend.shell_list_tabs()
        assert len(tabs) == 1
        assert tabs[0]["name"] == "default"

    def test_shell_format_tab_header(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        self._setup_exec_mock(mock_ssh)

        with patch("time.sleep"):
            backend._init_shell()

        header = backend.shell_format_tab_header()
        assert "default" in header
        assert "[Shells:" in header

    def test_shell_open_tab(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        self._setup_exec_mock(mock_ssh)

        with patch("time.sleep"):
            backend._init_shell()
            meta = backend.shell_open_tab("build")

        assert meta["name"] == "build"
        assert "build" in backend._tabs

    def test_shell_open_duplicate_raises(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        self._setup_exec_mock(mock_ssh)

        with patch("time.sleep"):
            backend._init_shell()
            with pytest.raises(ValueError, match="already exists"):
                backend.shell_open_tab("default")

    def test_shell_open_invalid_name_raises(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        self._setup_exec_mock(mock_ssh)

        with patch("time.sleep"):
            backend._init_shell()
            with pytest.raises(ValueError, match="Invalid tab name"):
                backend.shell_open_tab("UPPERCASE")

    def test_shell_open_exceeds_max_tabs_raises(self):
        mock_ssh = MagicMock()
        mock_sftp = MagicMock()
        transport = MagicMock()
        transport.is_active.return_value = True
        mock_ssh.get_transport.return_value = transport
        mock_ssh.open_sftp.return_value = mock_sftp

        _wire_exec_channel(mock_ssh, _WindowedChannel())

        with patch("paramiko.SSHClient", return_value=mock_ssh):
            backend = RemoteBackend(
                host="host", workspace_path="/ws", max_tabs=2, max_retries=1
            )
            backend.connect()
            with patch("time.sleep"):
                backend._init_shell()  # creates "default" tab (1/2)
                backend.shell_open_tab("second")  # (2/2)
                with pytest.raises(ValueError, match="Maximum tabs"):
                    backend.shell_open_tab("third")

    def test_shell_close_tab(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        self._setup_exec_mock(mock_ssh)

        with patch("time.sleep"):
            backend._init_shell()
            backend.shell_open_tab("temp")
            result = backend.shell_close_tab("temp")

        assert "closed" in result
        assert "temp" not in backend._tabs

    def test_shell_close_nonexistent_raises(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        self._setup_exec_mock(mock_ssh)

        with patch("time.sleep"):
            backend._init_shell()
            with pytest.raises(KeyError, match="not found"):
                backend.shell_close_tab("ghost")

    def test_shell_ensure_tab_creates_if_missing(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        self._setup_exec_mock(mock_ssh)

        with patch("time.sleep"):
            backend._init_shell()
            backend.shell_ensure_tab("auto")

        assert "auto" in backend._tabs

    def test_shell_ensure_tab_noop_if_exists(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        self._setup_exec_mock(mock_ssh)

        with patch("time.sleep"):
            backend._init_shell()
            # default already exists; should not raise or create duplicate
            backend.shell_ensure_tab("default")

        assert list(backend._tabs.keys()).count("default") == 1

    def test_shell_is_alive(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        # Setup exec for init (empty — _init_shell discards its own output)
        _wire_exec_channel(mock_ssh, _WindowedChannel())

        with patch("time.sleep"):
            backend._init_shell()

        # Re-wire with the is_alive check's actual output: the channel above
        # is a single-shot drain, already consumed by _init_shell's _exec calls.
        _wire_exec_channel(mock_ssh, _WindowedChannel(stdout_data=b"yes\n"))

        assert backend.shell_is_alive() is True

    def test_shell_is_alive_not_initialized(self, remote_backend):
        backend, _, _ = remote_backend
        assert backend.shell_is_alive() is False

    def test_shell_cleanup(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        _wire_exec_channel(mock_ssh, _WindowedChannel())

        with patch("time.sleep"):
            backend._init_shell()

        backend.shell_cleanup()
        assert backend._shell_initialized is False
        assert len(backend._tabs) == 0

    def test_shell_cleanup_not_initialized(self, remote_backend):
        backend, _, _ = remote_backend
        backend.shell_cleanup()  # Should not raise


class TestRemoteBackendShellSend:
    """Tests for RemoteBackend.shell_send()."""

    def _setup_exec_mock(self, mock_ssh, output: str = "", exit_code: int = 0):
        _wire_exec_channel(
            mock_ssh,
            _WindowedChannel(stdout_data=output.encode("utf-8"), exit_code=exit_code),
        )

    def test_send_to_existing_tab(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        self._setup_exec_mock(mock_ssh)

        with patch("time.sleep"):
            backend._init_shell()
            result = backend.shell_send("default", "ls -la")

        assert "Sent" in result

    def test_send_to_nonexistent_tab_raises(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        self._setup_exec_mock(mock_ssh)

        with patch("time.sleep"):
            backend._init_shell()
            with pytest.raises(KeyError, match="not found"):
                backend.shell_send("ghost", "ls")

    def test_send_blocked_command_with_enter(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        self._setup_exec_mock(mock_ssh)

        with patch("time.sleep"):
            backend._init_shell()
            result = backend.shell_send("default", "reboot", enter=True)

        assert "blocked" in result.lower()

    def test_send_blocked_command_without_enter_allowed(self, remote_backend):
        """When enter=False, blocked command check is skipped (just keystrokes)."""
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()
        self._setup_exec_mock(mock_ssh)

        with patch("time.sleep"):
            backend._init_shell()
            result = backend.shell_send("default", "reboot", enter=False)

        assert "Sent" in result


class TestRemoteBackendShellRead:
    """Tests for RemoteBackend.shell_read() and shell_read_with_offset()."""

    def _setup_exec_mock(self, mock_ssh, output: str = "", exit_code: int = 0):
        _wire_exec_channel(
            mock_ssh,
            _WindowedChannel(stdout_data=output.encode("utf-8"), exit_code=exit_code),
        )

    def test_shell_read_tail_mode(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        # Init shell with empty output
        self._setup_exec_mock(mock_ssh, "")
        with patch("time.sleep"):
            backend._init_shell()

        # Now set up capture output
        lines = "\n".join([f"line {i}" for i in range(20)])
        self._setup_exec_mock(mock_ssh, lines)

        text, meta = backend.shell_read("default", lines=5)
        assert meta["tab"] == "default"
        assert meta["mode"] == "tail"

    def test_shell_read_since_cursor(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        self._setup_exec_mock(mock_ssh, "")
        with patch("time.sleep"):
            backend._init_shell()

        lines = "\n".join([f"line {i}" for i in range(10)])
        self._setup_exec_mock(mock_ssh, lines)

        text, meta = backend.shell_read("default", since_cursor=True)
        assert meta["mode"] == "since_cursor"

    def test_shell_read_nonexistent_tab_raises(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        self._setup_exec_mock(mock_ssh, "")
        with patch("time.sleep"):
            backend._init_shell()
            with pytest.raises(KeyError, match="not found"):
                backend.shell_read("ghost")

    def test_shell_read_with_offset(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        self._setup_exec_mock(mock_ssh, "")
        with patch("time.sleep"):
            backend._init_shell()

        lines = "\n".join([f"line {i}" for i in range(50)])
        self._setup_exec_mock(mock_ssh, lines)

        text, meta = backend.shell_read_with_offset("default", lines=10, offset=5)
        assert meta["mode"] == "offset"
        assert meta["offset"] == 5

    def test_shell_read_with_offset_tail(self, remote_backend):
        backend, mock_ssh, mock_sftp = remote_backend
        backend.connect()

        self._setup_exec_mock(mock_ssh, "")
        with patch("time.sleep"):
            backend._init_shell()

        lines = "\n".join([f"line {i}" for i in range(50)])
        self._setup_exec_mock(mock_ssh, lines)

        text, meta = backend.shell_read_with_offset("default", lines=10, offset=None)
        assert meta["mode"] == "tail"


class TestRemoteBackendRemoteTab:
    """Tests for the _RemoteTab helper class."""

    def test_tab_metadata(self):
        tab = _RemoteTab(name="test-tab", tab_type="shell")
        meta = tab.to_metadata()
        assert meta["name"] == "test-tab"
        assert meta["type"] == "shell"
        assert "created_at" in meta
        assert "last_activity" in meta

    def test_tab_defaults(self):
        tab = _RemoteTab(name="default")
        assert tab.tab_type == "shell"
        assert tab.read_cursor == 0


# =============================================================================
# Backend interface compliance
# =============================================================================


class TestBackendInterfaceCompliance:
    """Verify RemoteBackend implements the full abstract interface."""

    def test_remote_backend_has_all_abstract_methods(self):
        backend = RemoteBackend(host="host", workspace_path="/ws")
        abstract_methods = [
            "read_file",
            "write_file",
            "append_file",
            "exists",
            "is_file",
            "is_dir",
            "list_dir",
            "search_files",
            "mkdir",
            "delete_file",
            "delete_directory",
            "move",
            "copy",
            "stat",
            "resolve_path",
            "connect",
            "disconnect",
            "is_connected",
            "root",
        ]
        for method in abstract_methods:
            assert hasattr(backend, method), f"RemoteBackend missing {method}"

    def test_remote_backend_has_shell_methods(self):
        backend = RemoteBackend(host="host", workspace_path="/ws")
        shell_methods = [
            "shell_run",
            "shell_send",
            "shell_read",
            "shell_read_with_offset",
            "shell_ensure_tab",
            "shell_open_tab",
            "shell_close_tab",
            "shell_list_tabs",
            "shell_format_tab_header",
            "shell_cleanup",
            "shell_is_alive",
        ]
        for method in shell_methods:
            assert hasattr(backend, method), f"RemoteBackend missing {method}"
