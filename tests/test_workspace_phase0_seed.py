"""Workspace seeding regressions: instructions clobber, job README, Phase 0 commit.

Covers the remote-backend instructions.md clobber (user-provided instructions
landed on the workspace, then _deploy_instruction_files overwrote them with
the template because its guard called .exists() on a local Path for a path
that only exists on the remote host), plus the Phase 0 seed behavior
(README rewrite + seed commit).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.core.workspace import WorkspaceManager
from tests._fs_backend import FilesystemTestBackend


class RemoteLikeBackend(FilesystemTestBackend):
    """Filesystem backend that resolves to a path that never exists locally.

    Models the production RemoteBackend property that broke the guard:
    all I/O works through the backend, but ``resolve_path()`` names a path
    on the remote host, so ``Path(resolve_path(...)).exists()`` is always
    False on the agent pod.
    """

    def resolve_path(self, relative_path: str) -> str:
        return f"/nonexistent-remote-host/workspace/{relative_path}"


def _bare_agent(workspace_manager, config=None):
    """UniversalAgent shell (no __init__) for unit-testing instance methods."""
    from src.agent import UniversalAgent

    agent = UniversalAgent.__new__(UniversalAgent)
    agent._workspace_manager = workspace_manager
    agent._agent_seed_files = {}
    agent.config = config or SimpleNamespace(
        llm=SimpleNamespace(model="test-model"),
        instruction_files=[],
        extra={},
    )
    return agent


class TestInstructionsClobber:
    """User-provided instructions.md must survive _deploy_instruction_files."""

    def test_custom_instructions_survive_on_remote_backend(self, tmp_path):
        ws = WorkspaceManager(job_id="t", backend=RemoteLikeBackend(tmp_path))
        ws.write_file("instructions.md", "CUSTOM USER BRIEF")
        agent = _bare_agent(ws)

        agent._deploy_instruction_files([])

        assert ws.read_file("instructions.md") == "CUSTOM USER BRIEF"

    def test_template_deployed_when_instructions_missing(self, tmp_path, monkeypatch):
        import src.core.loader as loader

        monkeypatch.setattr(loader, "load_instructions", lambda *a, **k: "TEMPLATE")
        monkeypatch.setattr(
            loader, "render_instruction_content", lambda content, *a, **k: content
        )
        ws = WorkspaceManager(job_id="t", backend=RemoteLikeBackend(tmp_path))
        agent = _bare_agent(ws)

        agent._deploy_instruction_files([])

        assert ws.read_file("instructions.md") == "TEMPLATE"


class TestJobReadme:
    """README seeding: replace the Gitea stub, never a real README."""

    METADATA = {
        "description": "Analyze the quarterly reports.",
        "config_name": "scholar",
    }

    def test_gitea_stub_is_replaced_with_description(self, tmp_path):
        ws = WorkspaceManager(job_id="t", backend=FilesystemTestBackend(tmp_path))
        ws.write_file("README.md", "# job-ab12cd34\n\nWorkspace for job-ab12cd34")
        agent = _bare_agent(ws)

        agent._write_job_readme("ab12cd34-0000-0000-0000-000000000000", self.METADATA)

        content = ws.read_file("README.md")
        assert content.startswith("# Job ab12cd34")
        assert "Analyze the quarterly reports." in content
        assert agent._agent_seed_files["README.md"] == content

    def test_real_readme_is_never_touched(self, tmp_path):
        ws = WorkspaceManager(job_id="t", backend=FilesystemTestBackend(tmp_path))
        original = "# My Project\n\nHand-written project docs."
        ws.write_file("README.md", original)
        agent = _bare_agent(ws)

        agent._write_job_readme("ab12cd34-0000-0000-0000-000000000000", self.METADATA)

        assert ws.read_file("README.md") == original
        assert "README.md" not in agent._agent_seed_files

    def test_readme_created_when_absent(self, tmp_path):
        ws = WorkspaceManager(job_id="t", backend=FilesystemTestBackend(tmp_path))
        agent = _bare_agent(ws)

        agent._write_job_readme("ab12cd34-0000-0000-0000-000000000000", self.METADATA)

        content = ws.read_file("README.md")
        assert "Analyze the quarterly reports." in content

    def test_empty_description_still_yields_readme(self, tmp_path):
        ws = WorkspaceManager(job_id="t", backend=FilesystemTestBackend(tmp_path))
        agent = _bare_agent(ws)

        agent._write_job_readme("ab12cd34-0000-0000-0000-000000000000", {})

        assert ws.read_file("README.md").startswith("# Job ab12cd34")


class TestPhase0SeedCommit:
    def _agent_with_git(self, git_manager):
        ws = MagicMock()
        ws.git_manager = git_manager
        return _bare_agent(ws)

    def test_commits_and_pushes_seeded_workspace(self):
        git = MagicMock()
        git.is_active = True
        git.commit.return_value = True
        agent = self._agent_with_git(git)

        agent._commit_workspace_seed("job-1")

        git.commit.assert_called_once()
        message = git.commit.call_args.args[0]
        assert message.startswith("[Phase 0 Seed]")
        assert git.commit.call_args.kwargs.get("allow_empty") is False
        git.push.assert_called_once()

    def test_no_push_when_nothing_to_commit(self):
        git = MagicMock()
        git.is_active = True
        git.commit.return_value = False
        agent = self._agent_with_git(git)

        agent._commit_workspace_seed("job-1")

        git.push.assert_not_called()

    def test_noop_without_git_manager(self):
        agent = self._agent_with_git(None)
        agent._commit_workspace_seed("job-1")  # must not raise

    def test_git_errors_are_non_fatal(self):
        git = MagicMock()
        git.is_active = True
        git.commit.side_effect = RuntimeError("boom")
        agent = self._agent_with_git(git)

        agent._commit_workspace_seed("job-1")  # must not raise


class TestPersistentSessionSkillGuard:
    """Session resume must not clobber an already-deployed (possibly
    user-edited) bound skill file on a remote workspace."""

    def test_bound_skill_not_overwritten_on_remote_backend(self, tmp_path):
        pytest.importorskip("langgraph")
        from src.core.loader import InstructionFileEntry
        from tests.test_persistent_session import _make_config, _make_session

        cfg = _make_config(
            extra={
                "_resolved_skills": {},
                "_resolved_instructions": {"cite-as-you-write": "FRESH TEMPLATE"},
            }
        )
        cfg._deployment_dir = None
        cfg.instruction_files = [
            InstructionFileEntry(
                trigger="before_tool:cite_web", skill="cite-as-you-write"
            )
        ]
        session = _make_session(config=cfg)
        session.workspace_manager = WorkspaceManager(
            job_id="t", backend=RemoteLikeBackend(tmp_path)
        )
        path = "skills/cite-as-you-write/SKILL.md"
        session.workspace_manager.write_file(path, "USER EDITED")

        session._deploy_instruction_files()

        assert session.workspace_manager.read_file(path) == "USER EDITED"
