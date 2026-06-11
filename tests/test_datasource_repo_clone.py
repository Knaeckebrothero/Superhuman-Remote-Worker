"""Tests for repository-datasource cloning (workspace-backend only).

Repository datasources clone exclusively on the workspace backend via
clone_repository_datasources(); the former agent-local subprocess
``git clone`` branch (setup_repository_datasource) was removed — it wrote
credentials and repos onto the agent pod (no_workspace_agent_mode.md §9.4).
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from src.core.datasource_setup import (
    clone_repository_datasources,
    inject_datasource_index,
    process_datasources,
    resolve_repo_clone_names,
)


def make_workspace_manager(supports_shell=True, with_backend=True):
    """Mock WorkspaceManager with a shell-capable remote backend."""
    ws = MagicMock()
    ws.path = Path("/tmp/ws")
    ws.source_repos = {}
    if not with_backend:
        ws.backend = None
        return ws
    backend = MagicMock()
    backend.supports_shell = supports_shell
    backend.resolve_home_path = MagicMock(
        side_effect=lambda rel: f"/home/agent-host/{rel}"
    )
    backend.shell_run = MagicMock(return_value="Exit code: 0")
    ws.backend = backend
    return ws


def token_ds(name="My Repo", url="https://github.com/org/repo.git", **extra):
    return {
        "type": "repository",
        "name": name,
        "connection_url": url,
        "credentials": {"auth_method": "token", "token": "tok123"},
        **extra,
    }


class TestCapabilityGate:
    """No shell-capable backend → loud skip, never a local clone."""

    def test_no_backend_skips_without_clone(self, caplog):
        ws = make_workspace_manager(with_backend=False)
        with patch("src.managers.git_manager.GitManager.clone") as mock_clone:
            clone_repository_datasources([token_ds()], ws)
        mock_clone.assert_not_called()
        assert ws.source_repos == {}
        assert any("no local clone" in r.message for r in caplog.records)

    def test_backend_without_shell_skips(self, caplog):
        ws = make_workspace_manager(supports_shell=False)
        with patch("src.managers.git_manager.GitManager.clone") as mock_clone:
            clone_repository_datasources([token_ds()], ws)
        mock_clone.assert_not_called()
        assert any("shell support" in r.message for r in caplog.records)

    def test_empty_list_is_noop(self):
        ws = make_workspace_manager(with_backend=False)
        clone_repository_datasources([], ws)  # must not raise or log errors

    def test_local_clone_function_removed(self):
        from src.core import datasource_setup

        assert not hasattr(datasource_setup, "setup_repository_datasource")


class TestBackendClone:
    """Clones run via GitManager.clone(backend=...) on the workspace."""

    def test_token_auth_injects_url_and_registers(self):
        ws = make_workspace_manager()
        git_mgr = MagicMock()
        with patch(
            "src.managers.git_manager.GitManager.clone", return_value=git_mgr
        ) as mock_clone:
            clone_repository_datasources(
                [token_ds(default_branch="dev")],
                ws,
            )

        mock_clone.assert_called_once()
        url_arg = mock_clone.call_args[0][0]
        assert "oauth2:tok123@github.com" in url_arg
        assert mock_clone.call_args[1]["backend"] is ws.backend
        assert mock_clone.call_args[1]["remote_cwd"] == "repos/repo"
        git_mgr.checkout_branch.assert_called_once_with("dev")
        assert ws.source_repos["repo"] is git_mgr

    def test_ssh_auth_writes_key_on_backend_and_converts_url(self):
        ws = make_workspace_manager()
        ds = {
            "type": "repository",
            "name": "My Repo",
            "connection_url": "https://github.com/org/repo.git",
            "credentials": {"auth_method": "ssh", "ssh_key": "KEYMATERIAL"},
        }
        with patch(
            "src.managers.git_manager.GitManager.clone", return_value=MagicMock()
        ) as mock_clone:
            clone_repository_datasources([ds], ws)

        # Key written to the workspace home (normalized with trailing newline)
        ws.backend.write_home_file.assert_called_once_with(
            ".ssh/repo_my-repo", "KEYMATERIAL\n"
        )
        # chmod + ssh config appended via shell on the workspace
        shell_cmds = [c[0][0] for c in ws.backend.shell_run.call_args_list]
        assert any("mkdir -p ~/.ssh" in cmd for cmd in shell_cmds)
        assert any("chmod 600" in cmd for cmd in shell_cmds)
        assert any(">> ~/.ssh/config" in cmd for cmd in shell_cmds)
        # HTTPS URL converted to SSH form so git uses the key
        assert mock_clone.call_args[0][0] == "git@github.com:org/repo.git"

    def test_name_collision_gets_suffix(self):
        ws = make_workspace_manager()
        with patch(
            "src.managers.git_manager.GitManager.clone", return_value=MagicMock()
        ) as mock_clone:
            clone_repository_datasources(
                [token_ds(name="upstream"), token_ds(name="fork")],
                ws,
            )

        cwds = [c[1]["remote_cwd"] for c in mock_clone.call_args_list]
        assert cwds == ["repos/repo", "repos/repo-2"]
        assert set(ws.source_repos) == {"repo", "repo-2"}

    def test_failed_clone_not_registered(self, caplog):
        ws = make_workspace_manager()
        with patch("src.managers.git_manager.GitManager.clone", return_value=None):
            clone_repository_datasources([token_ds()], ws)
        assert ws.source_repos == {}
        assert any("Failed to clone" in r.message for r in caplog.records)


class TestResolveRepoCloneNames:
    """Clone-directory names: upstream repo name, label fallback, suffixes."""

    def test_uses_upstream_repo_name_not_label(self):
        names = resolve_repo_clone_names([token_ds(name="Read-only mirror")])
        assert names == ["repo"]

    def test_falls_back_to_label_slug_without_usable_url(self):
        names = resolve_repo_clone_names([token_ds(name="My Repo!", url="")])
        assert names == ["my-repo"]

    def test_collision_gets_suffix(self):
        names = resolve_repo_clone_names(
            [token_ds(name="upstream"), token_ds(name="fork")]
        )
        assert names == ["repo", "repo-2"]


class TestDatasourceIndexRepoPaths:
    """datasources.md must point at the directories clones actually land in."""

    def test_index_uses_clone_directory_names(self):
        ws = MagicMock()
        ws.read_file.side_effect = FileNotFoundError
        written = {}
        ws.write_file.side_effect = lambda path, content: written.update(
            {path: content}
        )

        inject_datasource_index([token_ds(name="Read-only mirror of upstream")], ws)

        content = written["datasources.md"]
        assert "`./repos/repo/`" in content
        # The old behavior used the datasource-label slug, which never
        # matched the clone directory (upstream repo name).
        assert "read-only-mirror-of-upstream" not in content


class TestProcessDatasourcesRepoGuard:
    """process_datasources never clones — repository entries are skipped."""

    def test_repository_ds_ignored_with_warning(self, caplog):
        with patch("src.core.datasource_setup.subprocess.run") as mock_run:
            connections, clients, cli_types = process_datasources([token_ds()])
        mock_run.assert_not_called()
        assert connections == {}
        assert clients == {}
        assert cli_types == []
        assert any(
            "ignored by process_datasources" in r.message for r in caplog.records
        )
