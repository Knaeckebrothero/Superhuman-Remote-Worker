"""A new harness assignment must preserve the workspace it was given."""

import subprocess

import pytest

from agent.core.workspace import WorkspaceManager, WorkspaceManagerConfig
from agent.managers.git_manager import GitManager
from tests._fs_backend import FilesystemTestBackend


class ShellFixture(FilesystemTestBackend):
    """Exercise the remote-shell branch against a disposable test directory."""

    supports_shell = True

    def shell_run(self, command, timeout=30, **kwargs):
        result = subprocess.run(
            ["sh", "-c", command],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return (
            f"Exit code: {result.returncode}\n--- stdout ---\n{result.stdout}"
            f"{result.stderr}"
        )


def manager(root, *, git=True, remote=None):
    return WorkspaceManager(
        job_id="new-assignment",
        base_path=root,
        backend=ShellFixture(root),
        config=WorkspaceManagerConfig(
            git_versioning=git, git_remote_url=remote, structure=["output", "archive"]
        ),
    )


@pytest.mark.parametrize("git", [False, True])
def test_initialization_and_previous_assignment_files_survive_new_harness(
    tmp_path, git
):
    files = {
        ".srw-initialize-count": "initialized\n",
        "previous-assignment.txt": "work to keep\n",
        ".cache/project/build-result": "compiled\n",
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    first = manager(tmp_path, git=git)
    first.initialize()
    for name, content in files.items():
        assert (tmp_path / name).read_text() == content
    commit = first.git_manager.get_current_commit() if git else None
    second = manager(tmp_path, git=git)
    second.initialize()
    for name, content in files.items():
        assert (tmp_path / name).read_text() == content
    if git:
        assert second.git_manager.get_current_commit() == commit
    assert (tmp_path / "output").is_dir()


@pytest.mark.parametrize("matching", [True, False])
def test_existing_delivery_repository_is_reused_or_refused_without_wiping(
    tmp_path, monkeypatch, matching
):
    existing = "https://example.invalid/owned/repository.git"
    original = manager(tmp_path)
    original.initialize()
    original.git_manager.add_remote("origin", existing)
    (tmp_path / "unfinished.txt").write_text("uncommitted work\n")
    commit = original.git_manager.get_current_commit()
    clones = []

    def no_network_clone(*args, **kwargs):
        clones.append(args)
        return None

    monkeypatch.setattr(GitManager, "clone", no_network_clone)
    expected = (
        existing if matching else "https://example.invalid/another/repository.git"
    )
    attached = manager(tmp_path, remote=expected)
    if matching:
        attached.initialize()
        assert attached.git_manager.get_current_commit() == commit
    else:
        with pytest.raises(RuntimeError, match="does not match"):
            attached.initialize()
    assert (tmp_path / "unfinished.txt").read_text() == "uncommitted work\n"
    assert original.git_manager.remote_url("origin") == existing
    assert not clones
