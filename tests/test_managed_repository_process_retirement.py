from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from orchestrator.services import managed_repository_process_retirement as subject


@pytest.mark.asyncio
async def test_whole_workspace_retirement_composes_valid_shell(monkeypatch):
    commands: list[str] = []

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def run(self, command, **_kwargs):
            commands.append(command)
            syntax = subprocess.run(
                ["bash", "-n"],
                input=command,
                text=True,
                capture_output=True,
                check=False,
            )
            return SimpleNamespace(exit_status=syntax.returncode)

        def close(self):
            return None

        async def wait_closed(self):
            return None

    class AsyncSSH:
        @staticmethod
        async def connect(*_args, **_kwargs):
            return Connection()

    monkeypatch.setattr(subject, "asyncssh", AsyncSSH())
    monkeypatch.setattr(subject, "resolve_ssh_key_path", lambda: "/test/key")

    assert await subject.retire_managed_repository_processes(
        host="192.0.2.1",
        port=30022,
        host_key_fingerprint="SHA256:" + "a" * 43,
    )
    assert len(commands) == 1
    assert "; ;" not in commands[0]
    assert " all " in commands[0]
    assert " zero " in commands[0]
