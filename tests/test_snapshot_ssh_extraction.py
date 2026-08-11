"""Contract tests for streaming snapshot restore (memory-safe extraction).

Guards against regressing to "read the whole .tar.zst into RAM, then pass it as
``communicate(input=...)``" — the pattern that OOM-killed the orchestrator on
resume. The helper must stream the local tar file to the child process via its
stdin file descriptor. The global ``asyncio.create_subprocess_exec`` is patched,
so no real SSH is spawned.
"""

import asyncio
import base64
import hashlib
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import paramiko
import pytest

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))
_orchestrator_dir = str(project_root / "orchestrator")
if _orchestrator_dir not in sys.path:
    sys.path.insert(0, _orchestrator_dir)

from orchestrator.services.ssh_helpers import (  # noqa: E402
    EXTRACT_REMOTE_CMD,
    SSHPrivateKeyError,
    _read_stream_tail,
    build_agent_ssh_cmd,
    stream_extract_snapshot,
    wait_for_agent_ssh,
    workspace_private_key_fingerprint,
)

VALID_TEST_FINGERPRINT = "SHA256:" + ("A" * 43)


def _fake_proc(returncode=0, stderr=b""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(b"", stderr))
    proc.wait = AsyncMock(return_value=returncode)
    proc.stderr.read = AsyncMock(side_effect=[stderr, b""])
    return proc


class TestWorkspacePrivateKeyFingerprint:
    def test_valid_key_returns_only_public_fingerprint(self, tmp_path):
        key_path = tmp_path / "id_rsa"
        paramiko.RSAKey.generate(1024).write_private_key_file(str(key_path))

        fingerprint = workspace_private_key_fingerprint(str(key_path))

        assert fingerprint.startswith("SHA256:")
        assert "PRIVATE KEY" not in fingerprint

    def test_missing_key_is_a_safe_configuration_error(self, tmp_path):
        with pytest.raises(SSHPrivateKeyError, match="does not exist"):
            workspace_private_key_fingerprint(str(tmp_path / "missing"))


@pytest.fixture
def tar_file():
    """A small real file so the helper's open() yields a genuine file object."""
    with tempfile.NamedTemporaryFile(suffix=".tar.zst", delete=True) as tmp:
        tmp.write(b"\x28\xb5\x2f\xfd" + b"payload" * 64)
        tmp.flush()
        yield tmp.name


class TestBuildAgentSshCmd:
    def test_includes_options_host_and_remote_cmd(self):
        cmd = build_agent_ssh_cmd(
            "10.0.0.5", 2222, EXTRACT_REMOTE_CMD, key_path="/tmp/key"
        )
        assert cmd[0] == "ssh"
        assert "-i" in cmd and "/tmp/key" in cmd
        assert "StrictHostKeyChecking=no" in cmd
        assert "UserKnownHostsFile=/dev/null" in cmd
        assert "ConnectTimeout=10" in cmd
        assert "-p" in cmd and "2222" in cmd
        assert "agent-host@10.0.0.5" in cmd
        assert cmd[-1] == EXTRACT_REMOTE_CMD

    def test_no_key_omits_i_flag(self):
        cmd = build_agent_ssh_cmd("h", 22, EXTRACT_REMOTE_CMD, key_path="")
        assert "-i" not in cmd

    def test_probe_options_are_explicit(self):
        cmd = build_agent_ssh_cmd(
            "h",
            22,
            "true",
            key_path="/tmp/key",
            connect_timeout_s=3,
            batch_mode=True,
        )
        assert "ConnectTimeout=3" in cmd
        assert "BatchMode=yes" in cmd
        assert "IdentitiesOnly=yes" in cmd
        assert "PreferredAuthentications=publickey" in cmd
        assert cmd[-1] == "true"


class TestWaitForAgentSsh:
    @pytest.mark.asyncio
    async def test_success_returns_attempt_count(self):
        fake = _fake_proc(returncode=0)
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake)):
            ready, attempts, error = await wait_for_agent_ssh(
                "10.0.0.9",
                22,
                deadline_s=1,
                connect_timeout_s=1,
                interval_s=0,
                key_path="/tmp/k",
            )

        assert ready is True
        assert attempts == 1
        assert error == ""

    @pytest.mark.asyncio
    async def test_failure_returns_last_error(self):
        fake = _fake_proc(returncode=255, stderr=b"ssh: connect timed out")
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake)):
            ready, attempts, error = await wait_for_agent_ssh(
                "10.0.0.9",
                22,
                deadline_s=0,
                connect_timeout_s=1,
                interval_s=0,
                key_path="/tmp/k",
            )

        assert ready is False
        assert attempts == 1
        assert "timed out" in error

    @pytest.mark.asyncio
    async def test_missing_ssh_binary_fails_safely_without_retry(self):
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=FileNotFoundError("ssh")),
        ):
            ready, attempts, error = await wait_for_agent_ssh(
                "10.0.0.9",
                22,
                deadline_s=30,
                connect_timeout_s=1,
                interval_s=1,
                key_path="/tmp/k",
            )

        assert ready is False
        assert attempts == 1
        assert error == "ssh readiness probe could not start: FileNotFoundError"


class TestStreamExtractSnapshot:
    @pytest.mark.asyncio
    async def test_streams_file_as_stdin_not_bytes(self, tar_file):
        fake = _fake_proc(returncode=0)
        with patch(
            "asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake)
        ) as mock_exec:
            rc, stderr = await stream_extract_snapshot(
                "10.0.0.9", 22, tar_file, key_path="/tmp/k"
            )

        assert rc == 0
        assert stderr == b""

        # stdin must be a streamed file object, never the file's bytes —
        # this is the regression guard against re-introducing f.read().
        stdin = mock_exec.call_args.kwargs["stdin"]
        assert not isinstance(stdin, (bytes, bytearray, memoryview))
        assert hasattr(stdin, "read") and hasattr(stdin, "fileno")

        # communicate() must be awaited with NO input= (else it buffers in RAM).
        fake.communicate.assert_awaited_once_with()

        # argv contract
        argv = mock_exec.call_args.args
        assert argv[0] == "ssh"
        assert "StrictHostKeyChecking=no" in argv
        assert "agent-host@10.0.0.9" in argv
        assert argv[-1] == EXTRACT_REMOTE_CMD
        # Verify extract command includes xattrs/acls for overlay whiteout round-trip
        assert (
            EXTRACT_REMOTE_CMD
            == "zstd -d | tar --xattrs --xattrs-include='*' --acls -xf - -C /"
        )

    @pytest.mark.asyncio
    async def test_returns_rc_and_stderr_on_failure(self, tar_file):
        fake = _fake_proc(returncode=1, stderr=b"tar: short read")
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake)):
            rc, stderr = await stream_extract_snapshot(
                "h", 22, tar_file, key_path="/tmp/k"
            )
        assert rc == 1
        assert stderr == b"tar: short read"

    @pytest.mark.asyncio
    async def test_strict_extract_pins_scanned_key_and_uses_pipefail(self, tar_file):
        key_blob = b"provisioner-attested-ed25519-host-key"
        encoded = base64.b64encode(key_blob).decode("ascii")
        fingerprint = "SHA256:" + base64.b64encode(
            hashlib.sha256(key_blob).digest()
        ).decode("ascii").rstrip("=")
        scan = _fake_proc()
        scan.communicate.return_value = (
            f"[10.0.0.9]:30022 ssh-ed25519 {encoded}\n".encode(),
            b"",
        )
        extract = _fake_proc()

        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=[scan, extract]),
        ) as mock_exec:
            rc, stderr = await stream_extract_snapshot(
                "10.0.0.9",
                30022,
                tar_file,
                key_path="/tmp/k",
                expected_host_key_fingerprint=fingerprint,
                require_pipefail=True,
            )

        assert (rc, stderr) == (0, b"")
        scan_argv = mock_exec.await_args_list[0].args
        assert scan_argv[:2] == ("ssh-keyscan", "-T")
        assert "30022" in scan_argv
        extract_argv = mock_exec.await_args_list[1].args
        assert (
            mock_exec.await_args_list[1].kwargs["stdout"] == asyncio.subprocess.DEVNULL
        )
        assert "StrictHostKeyChecking=yes" in extract_argv
        known_hosts = next(
            value for value in extract_argv if value.startswith("UserKnownHostsFile=")
        )
        assert known_hosts != "UserKnownHostsFile=/dev/null"
        assert extract_argv[-1].startswith(
            "flock -w 300 /tmp/.srw-terminal-snapshot-restore.lock "
        )
        assert "bash -o pipefail -c " in extract_argv[-1]
        assert "zstd -d | tar" in extract_argv[-1]

    @pytest.mark.asyncio
    async def test_strict_extract_cancellation_kills_and_reaps_ssh_child(
        self, tar_file
    ):
        wait_calls = 0

        async def _cancel_then_reap():
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls == 1:
                raise asyncio.CancelledError
            return 0

        extract = _fake_proc()
        extract.returncode = None
        extract.wait = AsyncMock(side_effect=_cancel_then_reap)
        extract.kill = MagicMock()

        with (
            patch(
                "orchestrator.services.ssh_helpers._scan_pinned_host_key",
                new=AsyncMock(return_value=("host ssh-ed25519 key", b"")),
            ),
            patch(
                "asyncio.create_subprocess_exec", new=AsyncMock(return_value=extract)
            ),
        ):
            with pytest.raises(asyncio.CancelledError):
                await stream_extract_snapshot(
                    "10.0.0.9",
                    30022,
                    tar_file,
                    key_path="/tmp/k",
                    expected_host_key_fingerprint=VALID_TEST_FINGERPRINT,
                    require_pipefail=True,
                )

        extract.kill.assert_called_once_with()
        assert extract.wait.await_count >= 1

    @pytest.mark.asyncio
    async def test_strict_extract_timeout_kills_and_reaps_ssh_child(
        self, tar_file, monkeypatch
    ):
        wait_calls = 0

        async def _blocked_communicate():
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls == 1:
                await asyncio.sleep(3600)
            return 0

        extract = _fake_proc()
        extract.returncode = None
        extract.wait = AsyncMock(side_effect=_blocked_communicate)
        extract.kill = MagicMock()
        monkeypatch.setenv("STATELESS_SNAPSHOT_RESTORE_TIMEOUT_S", "0.01")

        with (
            patch(
                "orchestrator.services.ssh_helpers._scan_pinned_host_key",
                new=AsyncMock(return_value=("host ssh-ed25519 key", b"")),
            ),
            patch(
                "asyncio.create_subprocess_exec", new=AsyncMock(return_value=extract)
            ),
        ):
            rc, stderr = await stream_extract_snapshot(
                "10.0.0.9",
                30022,
                tar_file,
                key_path="/tmp/k",
                expected_host_key_fingerprint=VALID_TEST_FINGERPRINT,
                require_pipefail=True,
            )

        assert rc == 124
        assert b"timed out" in stderr
        extract.kill.assert_called_once_with()
        assert extract.wait.await_count >= 1

    @pytest.mark.asyncio
    async def test_strict_extract_rejects_host_key_mismatch_before_bytes(
        self, tar_file
    ):
        encoded = base64.b64encode(b"different-host-key").decode("ascii")
        scan = _fake_proc()
        scan.communicate.return_value = (
            f"[10.0.0.9]:30022 ssh-ed25519 {encoded}\n".encode(),
            b"",
        )

        with patch(
            "asyncio.create_subprocess_exec", new=AsyncMock(return_value=scan)
        ) as mock_exec:
            rc, stderr = await stream_extract_snapshot(
                "10.0.0.9",
                30022,
                tar_file,
                key_path="/tmp/k",
                expected_host_key_fingerprint=VALID_TEST_FINGERPRINT,
                require_pipefail=True,
            )

        assert rc == 255
        assert b"did not match" in stderr
        assert mock_exec.await_count == 1

    @pytest.mark.asyncio
    async def test_cancelled_host_key_scan_kills_and_reaps_child(self, tar_file):
        scan = _fake_proc()
        scan.returncode = None
        scan.communicate = AsyncMock(side_effect=asyncio.CancelledError)
        scan.kill = MagicMock()
        scan.wait = AsyncMock(return_value=0)

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=scan)):
            with pytest.raises(asyncio.CancelledError):
                await stream_extract_snapshot(
                    "10.0.0.9",
                    30022,
                    tar_file,
                    key_path="/tmp/k",
                    expected_host_key_fingerprint=VALID_TEST_FINGERPRINT,
                    require_pipefail=True,
                )

        scan.kill.assert_called_once_with()
        scan.wait.assert_awaited_once_with()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "fingerprint",
        [
            None,
            "",
            "SHA256:",
            "md5:abc",
            "SHA256:bad value",
            "SHA256:" + ("A" * 42),
            "SHA256:" + ("A" * 42) + "=",
            "SHA256:" + ("A" * 42) + "_",
        ],
    )
    async def test_strict_extract_refuses_missing_or_malformed_pin_before_ssh(
        self, tar_file, fingerprint
    ):
        create = AsyncMock(side_effect=AssertionError("strict pin precedes SSH"))
        with patch("asyncio.create_subprocess_exec", new=create):
            rc, stderr = await stream_extract_snapshot(
                "10.0.0.9",
                30022,
                tar_file,
                key_path="/tmp/k",
                expected_host_key_fingerprint=fingerprint,
                require_pipefail=True,
            )

        assert rc == 255
        assert b"pinned SSH host key" in stderr
        create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_subprocess_stderr_tail_is_bounded(self):
        stream = MagicMock()
        stream.read = AsyncMock(
            side_effect=[b"a" * 40000, b"b" * 40000, b"c" * 40000, b""]
        )

        tail = await _read_stream_tail(stream, limit=65536)

        assert len(tail) == 65536
        assert tail == (b"b" * 25536) + (b"c" * 40000)
