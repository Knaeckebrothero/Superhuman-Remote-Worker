"""Contract tests for streaming snapshot restore (memory-safe extraction).

Guards against regressing to "read the whole .tar.zst into RAM, then pass it as
``communicate(input=...)``" — the pattern that OOM-killed the orchestrator on
resume. The helper must stream the local tar file to the child process via its
stdin file descriptor. The global ``asyncio.create_subprocess_exec`` is patched,
so no real SSH is spawned.
"""

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
    build_agent_ssh_cmd,
    stream_extract_snapshot,
    wait_for_agent_ssh,
    workspace_private_key_fingerprint,
)


def _fake_proc(returncode=0, stderr=b""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(b"", stderr))
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
