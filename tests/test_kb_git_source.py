"""Tests for provider-neutral, credential-safe OKF git sources."""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
import stat
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.services.kb_git_source import (
    GitCommandError,
    GitCommandTimeouts,
    GiteaKnowledgeGitSource,
    RemoteKnowledgeGitSource,
    redact_git_error,
    validate_git_auth_configuration,
    validate_git_remote_url,
    validate_git_remote_trust,
)


HEAD_SHA = "a" * 40


@pytest.fixture(autouse=True)
def _trust_test_git_hosts(monkeypatch):
    monkeypatch.setenv(
        "KB_GIT_ALLOWED_HOSTS",
        "git.example,gitlab.example,host,example.test",
    )


class _FakeProcess:
    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        hang: bool = False,
    ) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self._planned_returncode = returncode
        self.returncode: int | None = None
        self.killed = False
        self._hang = hang
        self._finished = asyncio.Event()
        if not hang:
            self.stdout.feed_data(stdout)
            self.stdout.feed_eof()
            self.stderr.feed_data(stderr)
            self.stderr.feed_eof()

    async def wait(self) -> int:
        if self._hang:
            await self._finished.wait()
        self.returncode = self._planned_returncode
        return self._planned_returncode

    def kill(self) -> None:
        self.killed = True
        self._planned_returncode = -9
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._finished.set()


def _head_process(*, stderr: bytes = b"", returncode: int = 0) -> _FakeProcess:
    return _FakeProcess(
        stdout=f"{HEAD_SHA}\trefs/heads/main\n".encode(),
        stderr=stderr,
        returncode=returncode,
    )


@pytest.mark.asyncio
async def test_public_https_head_uses_exec_and_no_auth_hook(tmp_path):
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def fake_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return _head_process()

    source = RemoteKnowledgeGitSource(
        "https://github.com/example/notes.git",
        branch="main",
        temp_parent=tmp_path,
    )
    with (
        patch.dict(
            os.environ,
            {
                "SSH_AUTH_SOCK": "/tmp/sentinel-agent.sock",
                "SSH_AGENT_PID": "4242",
                "SSH_ASKPASS": "/tmp/sentinel-askpass",
            },
        ),
        patch(
            "orchestrator.services.kb_git_source.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ),
    ):
        assert await source.get_head() == HEAD_SHA

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == (
        "git",
        "ls-remote",
        "--exit-code",
        "https://github.com/example/notes.git",
        "refs/heads/main",
    )
    env = kwargs["env"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_KEY_0"] == "http.followRedirects"
    assert env["GIT_CONFIG_VALUE_0"] == "false"
    assert "GIT_ASKPASS" not in env
    assert "GIT_SSH_COMMAND" not in env
    assert "SSH_AUTH_SOCK" not in env
    assert "SSH_AGENT_PID" not in env
    assert "SSH_ASKPASS" not in env
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_https_token_uses_secret_env_and_temporary_askpass(tmp_path):
    token = "sentinel-token-never-in-argv"
    captured: dict[str, object] = {}

    async def fake_exec(*args, **kwargs):
        captured["argv"] = args
        env = kwargs["env"]
        captured["env"] = env
        helper = Path(env["GIT_ASKPASS"])
        captured["helper"] = helper
        captured["helper_body"] = helper.read_text(encoding="utf-8")
        captured["helper_mode"] = stat.S_IMODE(helper.stat().st_mode)
        return _head_process()

    source = RemoteKnowledgeGitSource(
        "https://gitlab.example/org/notes.git",
        branch="main",
        credentials={"auth_method": "token", "token": token},
        temp_parent=tmp_path,
    )
    with patch(
        "orchestrator.services.kb_git_source.asyncio.create_subprocess_exec",
        side_effect=fake_exec,
    ):
        assert await source.get_head() == HEAD_SHA

    assert token not in " ".join(captured["argv"])
    assert captured["env"]["SRW_GIT_PASSWORD"] == token
    assert token not in captured["helper_body"]
    assert captured["helper_mode"] == 0o700
    assert not captured["helper"].exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_ssh_auth_uses_mode_0600_key_and_git_ssh_command(tmp_path):
    ssh_key = "-----BEGIN OPENSSH PRIVATE KEY-----\nsentinel-key-body\n-----END OPENSSH PRIVATE KEY-----"
    captured: dict[str, object] = {}

    async def fake_exec(*args, **kwargs):
        captured["argv"] = args
        env = kwargs["env"]
        captured["env"] = env
        ssh_command = shlex.split(env["GIT_SSH_COMMAND"])
        key_path = Path(ssh_command[ssh_command.index("-i") + 1])
        known_hosts_option = next(
            value for value in ssh_command if value.startswith("UserKnownHostsFile=")
        )
        known_hosts_path = Path(known_hosts_option.partition("=")[2])
        captured["key_path"] = key_path
        captured["key_body"] = key_path.read_text(encoding="utf-8")
        captured["key_mode"] = stat.S_IMODE(key_path.stat().st_mode)
        captured["known_hosts_path"] = known_hosts_path
        captured["known_hosts_body"] = known_hosts_path.read_text(encoding="utf-8")
        return _head_process()

    source = RemoteKnowledgeGitSource(
        "https://github.com/org/notes",
        branch="main",
        credentials={"auth_method": "ssh", "ssh_key": ssh_key},
        temp_parent=tmp_path,
    )
    with patch(
        "orchestrator.services.kb_git_source.asyncio.create_subprocess_exec",
        side_effect=fake_exec,
    ):
        assert await source.get_head() == HEAD_SHA

    assert "git@github.com:org/notes.git" in captured["argv"]
    assert ssh_key not in " ".join(captured["argv"])
    ssh_command = captured["env"]["GIT_SSH_COMMAND"]
    assert "-F /dev/null" in ssh_command
    assert "BatchMode=yes" in ssh_command
    assert "IdentitiesOnly=yes" in ssh_command
    assert "IdentityAgent=none" in ssh_command
    assert "ConnectTimeout=30" in ssh_command
    assert "GlobalKnownHostsFile=/dev/null" in ssh_command
    assert "UserKnownHostsFile=" in ssh_command
    assert "SSH_AUTH_SOCK" not in captured["env"]
    assert captured["key_body"] == f"{ssh_key}\n"
    assert captured["key_mode"] == 0o600
    assert captured["known_hosts_body"] == ""
    assert not captured["key_path"].exists()
    assert not captured["known_hosts_path"].exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_admin_pinned_known_hosts_enables_strict_ssh_verification(
    tmp_path, monkeypatch
):
    pinned = "github.com ssh-ed25519 AAAAC3NzaPinnedHostKey\n"
    monkeypatch.setenv("KB_GIT_SSH_KNOWN_HOSTS", pinned)
    captured: dict[str, str] = {}

    async def fake_exec(*args, **kwargs):
        command = shlex.split(kwargs["env"]["GIT_SSH_COMMAND"])
        option = next(
            value for value in command if value.startswith("UserKnownHostsFile=")
        )
        captured["command"] = kwargs["env"]["GIT_SSH_COMMAND"]
        captured["known_hosts"] = Path(option.partition("=")[2]).read_text()
        return _head_process()

    source = RemoteKnowledgeGitSource(
        "ssh://git@github.com/org/notes.git",
        branch="main",
        credentials={"auth_method": "ssh", "ssh_key": "deploy-key"},
        temp_parent=tmp_path,
    )
    with patch(
        "orchestrator.services.kb_git_source.asyncio.create_subprocess_exec",
        side_effect=fake_exec,
    ):
        assert await source.get_head() == HEAD_SHA

    assert "StrictHostKeyChecking=yes" in captured["command"]
    assert "StrictHostKeyChecking=accept-new" not in captured["command"]
    assert captured["known_hosts"] == pinned


@pytest.mark.asyncio
async def test_ssh_snapshot_connect_timeout_uses_fetch_budget(tmp_path):
    commands: list[tuple[tuple[object, ...], str]] = []

    async def fake_exec(*args, **kwargs):
        commands.append((args, kwargs["env"]["GIT_SSH_COMMAND"]))
        return _FakeProcess()

    source = RemoteKnowledgeGitSource(
        "ssh://git@github.com/org/notes.git",
        credentials={"auth_method": "ssh", "ssh_key": "deploy-key"},
        temp_parent=tmp_path,
        timeouts=GitCommandTimeouts(fetch=17.9),
    )
    with patch(
        "orchestrator.services.kb_git_source.asyncio.create_subprocess_exec",
        side_effect=fake_exec,
    ):
        async with source.snapshot(HEAD_SHA):
            pass

    assert commands
    fetch_commands = [command for args, command in commands if "fetch" in args]
    assert fetch_commands
    assert all("ConnectTimeout=17" in command for command in fetch_commands)
    local_commands = [command for args, command in commands if "fetch" not in args]
    assert all("ConnectTimeout=30" in command for command in local_commands)


@pytest.mark.asyncio
async def test_failed_command_redacts_token_and_url_userinfo(tmp_path):
    token = "sentinel-secret-token"
    stderr = (
        f"fatal: auth failed for https://oauth2:{token}@git.example/org/repo.git "
        f"using {token}"
    ).encode()
    source = RemoteKnowledgeGitSource(
        "https://git.example/org/repo.git",
        branch="main",
        credentials={"token": token},
        temp_parent=tmp_path,
    )
    with (
        patch(
            "orchestrator.services.kb_git_source.asyncio.create_subprocess_exec",
            return_value=_head_process(stderr=stderr, returncode=128),
        ),
        pytest.raises(GitCommandError) as exc_info,
    ):
        await source.get_head()

    message = str(exc_info.value)
    assert token not in message
    assert "oauth2" not in message
    assert "[REDACTED]" in message
    assert list(tmp_path.iterdir()) == []


def test_rejects_credentials_embedded_in_https_url():
    with pytest.raises(ValueError, match="must not be embedded"):
        RemoteKnowledgeGitSource("https://oauth2:secret@git.example/org/repo.git")


@pytest.mark.parametrize(
    "remote",
    [
        "file:///tmp/repo.git",
        "/tmp/repo.git",
        "ext::sh -c id",
        "helper::payload",
        "git://host/repo.git",
        "https://host/repo.git?token=secret",
        "user:password@host:repo.git",
        "--upload-pack=evil",
    ],
)
def test_public_datasource_validator_rejects_local_or_extensible_transports(remote):
    with pytest.raises(ValueError):
        validate_git_remote_url(remote, allow_local=False)


@pytest.mark.parametrize(
    "remote",
    [
        "ssh://-oProxyCommand=evil/repo.git",
        "ssh://-user@host/repo.git",
        "ssh://git@-oProxyCommand=evil/repo.git",
        "git@-option:repo.git",
        "https://-option/repo.git",
        "http://bad_host/repo.git",
        "ssh://git@host:not-a-port/repo.git",
        "https://host:0/repo.git",
    ],
)
def test_remote_validator_rejects_option_shaped_or_unsafe_authority(remote):
    with pytest.raises(ValueError):
        validate_git_remote_url(remote, allow_local=False)


def test_direct_test_adapter_can_opt_into_local_remote(tmp_path):
    assert validate_git_remote_url(str(tmp_path), allow_local=True) == str(tmp_path)
    with pytest.raises(ValueError):
        RemoteKnowledgeGitSource(str(tmp_path))
    assert RemoteKnowledgeGitSource(str(tmp_path), allow_local=True).label


def test_default_trust_policy_allows_known_public_providers(monkeypatch):
    monkeypatch.delenv("KB_GIT_ALLOWED_HOSTS", raising=False)
    validate_git_remote_trust("https://github.com/org/repo.git")
    validate_git_remote_trust("ssh://git@gitlab.com/org/repo.git")


@pytest.mark.parametrize(
    "remote",
    [
        "https://arbitrary.example/repo.git",
        "https://127.0.0.1/repo.git",
        "https://127.1/repo.git",
        "https://2130706433/repo.git",
        "https://[::1]/repo.git",
        "https://localhost/repo.git",
        "https://metadata.internal/repo.git",
    ],
)
def test_trust_policy_rejects_unapproved_or_literal_targets(remote):
    with pytest.raises(ValueError):
        validate_git_remote_trust(remote)


def test_admin_can_allow_exact_self_hosted_endpoint_but_not_other_ports(monkeypatch):
    monkeypatch.setenv(
        "KB_GIT_ALLOWED_HOSTS",
        "gitea.internal,gitea.internal:3000",
    )
    validate_git_remote_trust("https://gitea.internal/org/repo.git")
    validate_git_remote_trust("https://gitea.internal:3000/org/repo.git")
    with pytest.raises(ValueError, match="not trusted"):
        validate_git_remote_trust("https://gitea.internal:3001/org/repo.git")


@pytest.mark.parametrize(
    "configured",
    ["*", "*.internal", "127.0.0.1", "2130706433", "localhost"],
)
def test_admin_trust_configuration_rejects_wildcards_and_loopback(
    monkeypatch, configured
):
    monkeypatch.setenv("KB_GIT_ALLOWED_HOSTS", configured)
    with pytest.raises(ValueError):
        validate_git_remote_trust("https://github.com/org/repo.git")


def test_https_to_ssh_conversion_rechecks_the_effective_port(monkeypatch):
    monkeypatch.setenv("KB_GIT_ALLOWED_HOSTS", "gitea.internal:3000")
    with pytest.raises(ValueError, match="not trusted"):
        RemoteKnowledgeGitSource(
            "https://gitea.internal:3000/org/repo.git",
            credentials={"auth_method": "ssh", "ssh_key": "deploy-key"},
        )


@pytest.mark.parametrize(
    ("remote", "credentials", "expected"),
    [
        ("https://host/repo.git", {}, "public"),
        ("http://host/repo.git", {"auth_method": "public"}, "public"),
        ("https://host/repo.git", {"token": "secret"}, "token"),
        (
            "https://host/repo.git",
            {"auth_method": "password", "password": "secret"},
            "password",
        ),
        (
            "https://host/repo.git",
            {"auth_method": "ssh", "ssh_key": "deploy-key"},
            "ssh",
        ),
        (
            "ssh://git@host/repo.git",
            {"auth_method": "ssh", "ssh_key": "deploy-key"},
            "ssh",
        ),
        (
            "git@host:repo.git",
            {"auth_method": "ssh", "ssh_key": "deploy-key"},
            "ssh",
        ),
    ],
)
def test_git_auth_transport_matrix_accepts_supported_combinations(
    remote, credentials, expected
):
    assert validate_git_auth_configuration(remote, credentials) == expected


@pytest.mark.parametrize(
    ("remote", "credentials"),
    [
        ("http://host/repo.git", {"token": "secret"}),
        ("http://host/repo.git", {"password": "secret"}),
        ("ssh://git@host/repo.git", {}),
        ("git@host:repo.git", {"auth_method": "public"}),
        ("ssh://git@host/repo.git", {"auth_method": "token", "token": "x"}),
        ("git@host:repo.git", {"auth_method": "password", "password": "x"}),
        ("https://host/repo.git", {"auth_method": "kerberos"}),
        (
            "https://host/repo.git",
            {"auth_method": "public", "token": "hidden"},
        ),
        (
            "https://host/repo.git",
            {"auth_method": "public", "ssh_key": "hidden"},
        ),
        ("https://host/repo.git", {"auth_method": "token"}),
        ("https://host/repo.git", {"auth_method": "ssh"}),
        (
            "http://host/repo.git",
            {"auth_method": "ssh", "ssh_key": "deploy-key"},
        ),
    ],
)
def test_git_auth_transport_matrix_rejects_unsafe_combinations(remote, credentials):
    with pytest.raises(ValueError):
        validate_git_auth_configuration(remote, credentials)


@pytest.mark.parametrize(
    ("remote", "credentials"),
    [
        ("http://host/repo.git", {"token": "secret"}),
        ("ssh://git@host/repo.git", {}),
        ("git@host:repo.git", {"auth_method": "token", "token": "secret"}),
        ("https://arbitrary.example/repo.git", {}),
        (
            "https://host/repo.git",
            {"auth_method": "public", "token": "hidden"},
        ),
    ],
)
def test_remote_source_revalidates_auth_transport_defense_in_depth(remote, credentials):
    with pytest.raises(ValueError):
        RemoteKnowledgeGitSource(remote, credentials=credentials)


def test_redact_git_error_handles_encoded_secrets_and_scp_passwords():
    secret = "token with/slash"
    diagnostic = (
        "token%20with%2Fslash https://user:password@example.test/repo "
        "user:password@example.test:repo"
    )
    redacted = redact_git_error(diagnostic, (secret,))
    assert secret not in redacted
    assert "password" not in redacted
    assert "[REDACTED]" in redacted


@pytest.mark.asyncio
async def test_gitea_adapter_delegates_one_ref_snapshot():
    client = AsyncMock()
    client.get_branch_head_sha.return_value = HEAD_SHA
    client.list_tree.return_value = [
        {"path": "knowledge/note.md", "type": "blob", "sha": "b" * 40}
    ]
    client.get_file_content.return_value = "# Note\n"
    source = GiteaKnowledgeGitSource(
        client, "jobs-project", branch="job/main", label="native"
    )

    assert source.label == "native"
    assert await source.get_head() == HEAD_SHA
    async with source.snapshot(HEAD_SHA) as snapshot:
        assert (await snapshot.list_tree())[0]["path"] == "knowledge/note.md"
        assert await snapshot.get_file("knowledge/note.md") == "# Note\n"

    client.get_branch_head_sha.assert_awaited_once_with("jobs-project", "job/main")
    client.list_tree.assert_awaited_once_with("jobs-project", HEAD_SHA)
    client.get_file_content.assert_awaited_once_with(
        "jobs-project", "knowledge/note.md", ref=HEAD_SHA
    )


@pytest.mark.asyncio
async def test_snapshot_retries_without_filter_when_remote_rejects_partial_clone(
    tmp_path,
):
    calls: list[tuple[object, ...]] = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        if "--filter=blob:none" in args:
            return _FakeProcess(
                stderr=b"fatal: server does not support filter", returncode=128
            )
        return _FakeProcess()

    source = RemoteKnowledgeGitSource(
        "https://git.example/org/repo.git",
        branch="main",
        temp_parent=tmp_path,
    )
    with patch(
        "orchestrator.services.kb_git_source.asyncio.create_subprocess_exec",
        side_effect=fake_exec,
    ):
        async with source.snapshot(HEAD_SHA):
            pass

    fetch_calls = [call for call in calls if "fetch" in call]
    assert len(fetch_calls) == 2
    assert "--filter=blob:none" in fetch_calls[0]
    assert "--filter=blob:none" not in fetch_calls[1]
    assert list(tmp_path.iterdir()) == []


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(work: Path, message: str) -> None:
    _git("add", "-A", cwd=work)
    _git("commit", "-m", message, cwd=work)


@pytest.mark.asyncio
async def test_local_bare_repository_snapshot_tree_unicode_deletion_and_cleanup(
    tmp_path,
):
    work = tmp_path / "work"
    bare = tmp_path / "remote.git"
    snapshots = tmp_path / "snapshots"
    work.mkdir()
    snapshots.mkdir()
    _git("init", "--initial-branch=main", cwd=work)
    _git("config", "user.name", "KB Test", cwd=work)
    _git("config", "user.email", "kb@example.test", cwd=work)
    (work / "vault" / "nested").mkdir(parents=True)
    (work / "vault" / "welcome.md").write_text("# Grüß dich\n", encoding="utf-8")
    unicode_note = work / "vault" / "nested" / "über.md"
    unicode_note.write_text("# Über\n\nSchöne Grüße 🌍\n", encoding="utf-8")
    (work / "vault" / "ignored.txt").write_text("not markdown", encoding="utf-8")
    (work / "outside.md").write_text("# Outside\n", encoding="utf-8")
    if hasattr(os, "symlink"):
        os.symlink("../welcome.md", work / "vault" / "symlink.md")
    _commit(work, "initial knowledge")
    _git("init", "--bare", str(bare))
    _git("remote", "add", "origin", str(bare), cwd=work)
    _git("push", "-u", "origin", "main", cwd=work)

    source = RemoteKnowledgeGitSource(
        str(bare),
        branch="main",
        root_path="vault",
        temp_parent=snapshots,
        allow_local=True,
    )
    first_head = await source.get_head()
    assert first_head == _git("rev-parse", "HEAD", cwd=work)
    async with source.snapshot(first_head) as snapshot:
        tree = await snapshot.list_tree()
        assert {entry["path"] for entry in tree} == {
            "vault/nested/über.md",
            "vault/welcome.md",
        }
        assert await snapshot.get_file("vault/nested/über.md") == (
            "# Über\n\nSchöne Grüße 🌍\n"
        )
        assert await snapshot.get_file("outside.md") is None
        assert await snapshot.get_file("vault/symlink.md") is None
        assert any(snapshots.iterdir())
    assert list(snapshots.iterdir()) == []

    unicode_note.unlink()
    (work / "vault" / "welcome.md").write_text("# Updated\n", encoding="utf-8")
    _commit(work, "delete nested note")
    _git("push", "origin", "main", cwd=work)
    second_head = await source.get_head()
    assert second_head != first_head
    async with source.snapshot(second_head) as snapshot:
        assert {entry["path"] for entry in await snapshot.list_tree()} == {
            "vault/welcome.md"
        }
        assert await snapshot.get_file("vault/welcome.md") == "# Updated\n"
    assert list(snapshots.iterdir()) == []


@pytest.mark.asyncio
async def test_snapshot_timeout_kills_process_and_cleans_all_temp_files(tmp_path):
    processes: list[_FakeProcess] = []

    async def fake_exec(*args, **kwargs):
        process = _FakeProcess(hang=True)
        processes.append(process)
        return process

    source = RemoteKnowledgeGitSource(
        "https://git.example/org/repo.git",
        branch="main",
        temp_parent=tmp_path,
        timeouts=GitCommandTimeouts(head=0.01, fetch=0.01, tree=0.01, blob=0.01),
    )
    with (
        patch(
            "orchestrator.services.kb_git_source.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ),
        pytest.raises(GitCommandError, match="timed out"),
    ):
        async with source.snapshot(HEAD_SHA):
            pytest.fail("snapshot should not open")

    assert processes
    assert all(process.killed for process in processes)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_timeout_terminates_entire_git_process_group(tmp_path):
    process = _FakeProcess(hang=True)
    process.pid = 424242
    subprocess_kwargs: dict[str, object] = {}
    group_signals: list[tuple[int, int]] = []

    async def fake_exec(*args, **kwargs):
        subprocess_kwargs.update(kwargs)
        return process

    def fake_killpg(process_group: int, signal_number: int) -> None:
        group_signals.append((process_group, signal_number))
        if signal_number == signal.SIGKILL:
            process.kill()

    source = RemoteKnowledgeGitSource(
        "https://git.example/org/repo.git",
        branch="main",
        temp_parent=tmp_path,
        timeouts=GitCommandTimeouts(head=0.01),
    )
    with (
        patch(
            "orchestrator.services.kb_git_source.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ),
        patch(
            "orchestrator.services.kb_git_source.os.getpgid",
            return_value=process.pid,
        ),
        patch(
            "orchestrator.services.kb_git_source.os.killpg",
            side_effect=fake_killpg,
        ),
        pytest.raises(GitCommandError, match="timed out"),
    ):
        await source.get_head()

    assert subprocess_kwargs["start_new_session"] is True
    assert group_signals == [
        (process.pid, signal.SIGTERM),
        (process.pid, signal.SIGKILL),
    ]
    assert process.killed
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_leader_exit_after_term_still_force_kills_captured_process_group(
    tmp_path,
):
    process = _FakeProcess(hang=True)
    process.pid = 434343
    group_signals: list[tuple[int, int]] = []
    descendants_killed = False

    async def fake_exec(*args, **kwargs):
        return process

    def fake_killpg(process_group: int, signal_number: int) -> None:
        nonlocal descendants_killed
        group_signals.append((process_group, signal_number))
        if signal_number == signal.SIGTERM:
            # The Git leader obeys TERM immediately; a helper remains in the
            # captured group and deliberately ignores it.
            process._finished.set()
        elif signal_number == signal.SIGKILL:
            descendants_killed = True

    source = RemoteKnowledgeGitSource(
        "https://git.example/org/repo.git",
        branch="main",
        temp_parent=tmp_path,
        timeouts=GitCommandTimeouts(head=0.01),
    )
    with (
        patch(
            "orchestrator.services.kb_git_source.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ),
        patch(
            "orchestrator.services.kb_git_source.os.getpgid",
            return_value=process.pid,
        ) as getpgid,
        patch(
            "orchestrator.services.kb_git_source.os.killpg",
            side_effect=fake_killpg,
        ),
        pytest.raises(GitCommandError, match="timed out"),
    ):
        await source.get_head()

    getpgid.assert_called_once_with(process.pid)
    assert group_signals == [
        (process.pid, signal.SIGTERM),
        (process.pid, signal.SIGKILL),
    ]
    assert process.returncode == 0
    assert descendants_killed
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_snapshot_cancellation_kills_process_and_cleans_all_temp_files(tmp_path):
    started = asyncio.Event()
    process = _FakeProcess(hang=True)

    async def fake_exec(*args, **kwargs):
        started.set()
        return process

    source = RemoteKnowledgeGitSource(
        "https://git.example/org/repo.git",
        branch="main",
        temp_parent=tmp_path,
    )

    async def open_snapshot() -> None:
        async with source.snapshot(HEAD_SHA):
            pytest.fail("snapshot should not open")

    with patch(
        "orchestrator.services.kb_git_source.asyncio.create_subprocess_exec",
        side_effect=fake_exec,
    ):
        task = asyncio.create_task(open_snapshot())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert process.killed
    assert list(tmp_path.iterdir()) == []


def _make_archive(tmp_path: Path) -> Path:
    """A Gitea-shaped tar.gz: one top-level repo dir wrapping the tree."""
    import io
    import tarfile

    archive_path = tmp_path / "repo-head.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tar:
        for name, data in [
            ("jobs-project/knowledge/a.md", b"# A\n"),
            ("jobs-project/knowledge/b.md", b"# B\n"),
            ("jobs-project/knowledge/bad.md", b"\xff\xfe garbage"),
        ]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return archive_path


@pytest.mark.asyncio
async def test_gitea_snapshot_prefetch_serves_reads_from_archive(tmp_path):
    """After prefetch, get_file reads the tarball — zero per-file REST calls."""
    import shutil

    archive_src = _make_archive(tmp_path)
    client = AsyncMock()

    async def fake_download(repo_name, ref, dest_path):
        shutil.copy(archive_src, dest_path)
        return True

    client.download_repo_archive = AsyncMock(side_effect=fake_download)
    source = GiteaKnowledgeGitSource(client, "jobs-project")

    async with source.snapshot(HEAD_SHA) as snapshot:
        assert await snapshot.prefetch() is True
        assert await snapshot.prefetch() is True  # idempotent
        temp_path = snapshot._archive_path
        assert temp_path is not None and os.path.exists(temp_path)

        assert await snapshot.get_file("knowledge/a.md") == "# A\n"
        assert await snapshot.get_file("knowledge/b.md") == "# B\n"
        # Same contract as the REST path: undecodable/missing read as None.
        assert await snapshot.get_file("knowledge/bad.md") is None
        assert await snapshot.get_file("knowledge/missing.md") is None

    client.download_repo_archive.assert_awaited_once_with(
        "jobs-project", HEAD_SHA, temp_path
    )
    client.get_file_content.assert_not_called()
    assert not os.path.exists(temp_path)  # context exit removed the temp file


@pytest.mark.asyncio
async def test_archive_only_client_lazily_prefetches_for_single_file_read(tmp_path):
    """GitHub's approved read surface is tree + tarball, without contents GET."""
    import shutil

    archive_src = _make_archive(tmp_path)

    class ArchiveOnlyClient:
        def __init__(self):
            self.downloads = 0

        async def download_repo_archive(self, repo_name, ref, dest_path):
            assert (repo_name, ref) == ("jobs-project", HEAD_SHA)
            self.downloads += 1
            shutil.copy(archive_src, dest_path)
            return True

    client = ArchiveOnlyClient()
    source = GiteaKnowledgeGitSource(client, "jobs-project")

    async with source.snapshot(HEAD_SHA) as snapshot:
        assert await snapshot.get_file("knowledge/a.md") == "# A\n"
        assert await snapshot.get_file("knowledge/b.md") == "# B\n"

    assert client.downloads == 1


@pytest.mark.asyncio
async def test_gitea_snapshot_prefetch_failure_falls_back_to_rest():
    """A failed archive download degrades to the per-file path, never raises."""
    client = AsyncMock()
    client.download_repo_archive = AsyncMock(return_value=False)
    client.get_file_content = AsyncMock(return_value="# Note\n")
    source = GiteaKnowledgeGitSource(client, "jobs-project")

    async with source.snapshot(HEAD_SHA) as snapshot:
        assert await snapshot.prefetch() is False
        assert await snapshot.get_file("knowledge/a.md") == "# Note\n"

    client.get_file_content.assert_awaited_once_with(
        "jobs-project", "knowledge/a.md", ref=HEAD_SHA
    )


@pytest.mark.asyncio
async def test_gitea_snapshot_corrupt_archive_falls_back_to_rest(tmp_path):
    """A tarball that won't parse is discarded and reads stay on REST."""
    client = AsyncMock()

    async def fake_download(repo_name, ref, dest_path):
        Path(dest_path).write_bytes(b"not a tarball")
        return True

    client.download_repo_archive = AsyncMock(side_effect=fake_download)
    client.get_file_content = AsyncMock(return_value="# Note\n")
    source = GiteaKnowledgeGitSource(client, "jobs-project")

    async with source.snapshot(HEAD_SHA) as snapshot:
        assert await snapshot.prefetch() is False
        assert snapshot._archive_path is None  # temp file already cleaned up
        assert await snapshot.get_file("knowledge/a.md") == "# Note\n"
