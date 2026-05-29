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

import pytest

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))
_orchestrator_dir = str(project_root / "orchestrator")
if _orchestrator_dir not in sys.path:
    sys.path.insert(0, _orchestrator_dir)

from orchestrator.services.ssh_helpers import (  # noqa: E402
    EXTRACT_REMOTE_CMD,
    build_agent_ssh_cmd,
    stream_extract_snapshot,
)


def _fake_proc(returncode=0, stderr=b""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(b"", stderr))
    return proc


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

    @pytest.mark.asyncio
    async def test_returns_rc_and_stderr_on_failure(self, tar_file):
        fake = _fake_proc(returncode=1, stderr=b"tar: short read")
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake)):
            rc, stderr = await stream_extract_snapshot(
                "h", 22, tar_file, key_path="/tmp/k"
            )
        assert rc == 1
        assert stderr == b"tar: short read"
