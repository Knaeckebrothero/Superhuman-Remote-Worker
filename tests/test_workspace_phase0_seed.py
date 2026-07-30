"""Workspace seeding regressions: instructions clobber, job README, Phase 0 commit.

instructions.md is now a virtual file (docs/features/virtual_directories.md):
TestInstructionsClobber verifies the provider's inline > upload > template
precedence — the direct descendant of a real historical bug where
_deploy_instruction_files's guard called .exists() on a local Path for a path
that only ever exists on the remote host, unconditionally overwriting
user-provided instructions with the template. Plus the Phase 0 seed behavior
(README rewrite + seed commit).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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
    agent._job_metadata = None
    agent._resolved_instructions_md = None
    agent.config = config or SimpleNamespace(
        llm=SimpleNamespace(model="test-model"),
        instruction_files=[],
        extra={},
    )
    return agent


class TestInstructionsClobber:
    """User-provided instructions win over the template.

    instructions.md is virtual, so there is no exists()-probe left to fool —
    _deploy_instruction_files unconditionally registers a provider whose
    precedence (inline > upload > template) lives in
    build_instruction_providers(). This is the regression test for the bug
    that motivated the migration: a real .exists() probe on a local Path is
    always False for a remote backend, so the old guard clobbered
    user-provided instructions with the template on every remote job.
    """

    def test_custom_instructions_survive_on_remote_backend(self, tmp_path):
        ws = WorkspaceManager(job_id="t", backend=RemoteLikeBackend(tmp_path))
        agent = _bare_agent(ws)
        agent._job_metadata = {"instructions": "CUSTOM USER BRIEF"}

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


class TestResolveUploadedInstructions:
    """_resolve_uploaded_instructions() — the eager async resolution that lets
    a virtual instructions.md survive the resume paths in
    _setup_job_workspace. A virtual file persists nothing between runs, so
    the upload source (unlike inline, which is read live from metadata) must
    be resolved before any of that function's early returns, not lazily
    inside the provider — this is the unit-level half of that fix; the
    call-site (top-of-function, before any branch) is covered by the full
    existing _setup_job_workspace test suite passing unchanged.
    """

    @pytest.mark.asyncio
    async def test_no_upload_id_returns_none_without_any_io(self):
        agent = _bare_agent(MagicMock())
        agent._download_upload_files = AsyncMock(
            side_effect=AssertionError("must not attempt I/O without an upload id")
        )

        result = await agent._resolve_uploaded_instructions({"instructions": "INLINE"})

        assert result is None

    @pytest.mark.asyncio
    async def test_inline_short_circuits_even_when_upload_id_is_also_set(self):
        """Inline wins over upload (mirrors the deleted if/elif), so when both
        are present the download must never be attempted — regression test
        for a real bug: an earlier version resolved the upload unconditionally,
        paying an HTTP round-trip + local glob on every job with both fields
        set even though the result was always going to be discarded."""
        agent = _bare_agent(MagicMock())
        agent._download_upload_files = AsyncMock(
            side_effect=AssertionError("must not attempt I/O when inline is present")
        )

        result = await agent._resolve_uploaded_instructions(
            {"instructions": "INLINE", "instructions_upload_id": "up-1"}
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_http_download_wins_when_available(self):
        agent = _bare_agent(MagicMock())

        async def fake_download(upload_id, dest_dir, job_logger):
            (dest_dir / "instructions.md").write_text("FROM HTTP", encoding="utf-8")
            return ["instructions.md"]

        agent._download_upload_files = fake_download

        result = await agent._resolve_uploaded_instructions(
            {"instructions_upload_id": "up-1"}
        )

        assert result == "FROM HTTP"

    @pytest.mark.asyncio
    async def test_falls_back_to_local_uploads_dir_when_http_fails(
        self, tmp_path, monkeypatch
    ):
        import src.core.workspace as workspace_module

        monkeypatch.setattr(
            workspace_module, "get_workspace_base_path", lambda: tmp_path
        )
        local_dir = tmp_path / "uploads" / "up-2"
        local_dir.mkdir(parents=True)
        (local_dir / "instructions.md").write_text("FROM LOCAL", encoding="utf-8")

        agent = _bare_agent(MagicMock())
        agent._download_upload_files = AsyncMock(return_value=None)  # HTTP unavailable

        result = await agent._resolve_uploaded_instructions(
            {"instructions_upload_id": "up-2"}
        )

        assert result == "FROM LOCAL"

    @pytest.mark.asyncio
    async def test_returns_none_when_upload_id_resolves_to_nothing(
        self, tmp_path, monkeypatch
    ):
        import src.core.workspace as workspace_module

        monkeypatch.setattr(
            workspace_module, "get_workspace_base_path", lambda: tmp_path
        )
        agent = _bare_agent(MagicMock())
        agent._download_upload_files = AsyncMock(return_value=None)

        result = await agent._resolve_uploaded_instructions(
            {"instructions_upload_id": "missing-upload"}
        )

        assert result is None


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


class TestDocumentAutoRegistration:
    """Input-document auto-registration must discover files via the backend —
    a local Path walk never sees a remote workspace, which silently disabled
    registration on every remote-backend job."""

    def _context_for(self, ws):
        context = MagicMock()
        context.has_workspace.return_value = True
        context.vector_db = object()
        context.workspace_manager = ws
        context.get_or_register_doc_source = AsyncMock(return_value=1)
        return context

    @pytest.mark.asyncio
    async def test_registers_relative_paths_on_remote_backend(self, tmp_path):
        ws = WorkspaceManager(job_id="t", backend=RemoteLikeBackend(tmp_path))
        ws.write_file("documents/report.md", "content")
        ws.write_file("documents/sub/deep.pdf", "pdf bytes")
        ws.write_file("documents/external/page.html", "web content")  # skipped
        ws.write_file("documents/image.png", "img")  # unsupported extension
        agent = _bare_agent(ws)
        context = self._context_for(ws)

        await agent._register_initial_documents_background(context)

        registered = sorted(
            call.args[0] for call in context.get_or_register_doc_source.call_args_list
        )
        assert registered == ["documents/report.md", "documents/sub/deep.pdf"]

    @pytest.mark.asyncio
    async def test_missing_documents_dir_is_noop(self, tmp_path):
        ws = WorkspaceManager(job_id="t", backend=RemoteLikeBackend(tmp_path))
        agent = _bare_agent(ws)
        context = self._context_for(ws)

        await agent._register_initial_documents_background(context)

        context.get_or_register_doc_source.assert_not_called()

    @pytest.mark.asyncio
    async def test_registration_errors_are_non_fatal(self, tmp_path):
        ws = WorkspaceManager(job_id="t", backend=RemoteLikeBackend(tmp_path))
        ws.write_file("documents/report.md", "content")
        agent = _bare_agent(ws)
        context = self._context_for(ws)
        context.get_or_register_doc_source.side_effect = RuntimeError("boom")

        await agent._register_initial_documents_background(context)  # must not raise


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


class TestReseedFromSnapshotIfFresh:
    """The snapshot re-seed must fire on a fresh workspace and ONLY there.

    ``recover_to_phase`` overwrites checkpoint.db, plan.md, todos.yaml and
    archive/ (src/core/phase_snapshot.py). Firing it on a same-pod resume
    (cooldown pause/resume, freeze-continue, outage-sweeper redispatch)
    silently rewinds the job to the last phase boundary. The seeded-content
    marker is the only thing distinguishing the two cases, and probing a
    *virtual* task_brief.md would answer "unseeded" on every single resume.
    See docs/features/virtual_directories.md.
    """

    @staticmethod
    def _agent(tmp_path, backend):
        ws = WorkspaceManager(
            job_id="job-1",
            config=MagicMock(git_versioning=False),
            backend=backend,
            base_path=tmp_path,
        )
        agent = _bare_agent(ws)
        agent.config = SimpleNamespace(
            llm=SimpleNamespace(model="test-model"),
            instruction_files=[],
            extra={},
            workspace=SimpleNamespace(structure=["output"]),
        )
        return agent

    def test_seeded_workspace_does_not_rewind_to_the_last_snapshot(
        self, tmp_path, monkeypatch
    ):
        from src.core.backends.seed import mark_workspace_seeded

        backend = FilesystemTestBackend(tmp_path)
        agent = self._agent(tmp_path, backend)
        # A previous boot seeded this workspace; the pod never went away.
        mark_workspace_seeded(agent._workspace_manager.backend)

        snapshot_mgr = MagicMock()
        monkeypatch.setattr(
            "src.core.phase_snapshot.PhaseSnapshotManager",
            MagicMock(return_value=snapshot_mgr),
        )

        assert agent._reseed_from_snapshot_if_fresh("job-1", backend) is False
        snapshot_mgr.recover_to_phase.assert_not_called()
        snapshot_mgr.get_latest_snapshot.assert_not_called()

    def test_legacy_seeded_workspace_does_not_rewind_either(
        self, tmp_path, monkeypatch
    ):
        """No marker, but a real task_brief.md from a pre-marker release."""
        backend = FilesystemTestBackend(tmp_path)
        (tmp_path / "task_brief.md").write_text("# Task Brief\n")
        agent = self._agent(tmp_path, backend)

        snapshot_mgr = MagicMock()
        monkeypatch.setattr(
            "src.core.phase_snapshot.PhaseSnapshotManager",
            MagicMock(return_value=snapshot_mgr),
        )

        assert agent._reseed_from_snapshot_if_fresh("job-1", backend) is False
        snapshot_mgr.recover_to_phase.assert_not_called()

    def test_fresh_workspace_still_recovers_from_the_last_snapshot(
        self, tmp_path, monkeypatch
    ):
        """The safety net this branch exists for must still fire."""
        backend = FilesystemTestBackend(tmp_path)
        agent = self._agent(tmp_path, backend)

        snapshot_mgr = MagicMock()
        snapshot_mgr.get_latest_snapshot.return_value = SimpleNamespace(phase_number=3)
        monkeypatch.setattr(
            "src.core.phase_snapshot.PhaseSnapshotManager",
            MagicMock(return_value=snapshot_mgr),
        )

        assert agent._reseed_from_snapshot_if_fresh("job-1", backend) is True
        snapshot_mgr.recover_to_phase.assert_called_once()
        assert snapshot_mgr.recover_to_phase.call_args.args[0] == 3

    def test_a_virtual_task_brief_cannot_suppress_the_recovery(
        self, tmp_path, monkeypatch
    ):
        """The original hazard, inverted: virtual files must not read as seeded."""
        from src.core.virtual_dirs import SingleFileProvider

        backend = FilesystemTestBackend(tmp_path)
        agent = self._agent(tmp_path, backend)
        agent._workspace_manager.register_virtual_provider(
            SingleFileProvider("task_brief.md", lambda: "# Task Brief\n")
        )
        assert agent._workspace_manager.backend.exists("task_brief.md")

        snapshot_mgr = MagicMock()
        snapshot_mgr.get_latest_snapshot.return_value = SimpleNamespace(phase_number=1)
        monkeypatch.setattr(
            "src.core.phase_snapshot.PhaseSnapshotManager",
            MagicMock(return_value=snapshot_mgr),
        )

        assert (
            agent._reseed_from_snapshot_if_fresh(
                "job-1", agent._workspace_manager.backend
            )
            is True
        )
        snapshot_mgr.recover_to_phase.assert_called_once()
