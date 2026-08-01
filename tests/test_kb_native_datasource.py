"""The project's own knowledge base is indexed exactly once.

Step 6 of docs/features/knowledge_base_repo_separation.md gives every new
project a second managed repo (``project-<id8>-knowledge``) and auto-attaches
it as a ``kb`` datasource. That datasource is a *management surface* — visible,
listable, unlinkable — over notes that are already indexed under the project's
own id.

If it were mistaken for an ordinary external KB, the sweep would index the same
vault a second time under the datasource UUID and every note would appear twice
in search: acceptance criterion 5, and the one failure in that design that
corrupts search rather than merely failing it. The marker that prevents it is
``config.native_project_id``; these tests pin every place that reads it.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from main import (
    DatasourceUpdate,
    ProjectCreate,
    _normalize_kb_config,
    _provision_project_knowledge_repo,
    create_project,
    reindex_datasource_knowledge,
    update_datasource,
)
from orchestrator.services.kb_datasources import (
    NATIVE_PROJECT_CONFIG_KEY,
    native_kb_project_id,
    reindex_kb_datasource,
)
from orchestrator.services.kb_reindex import kb_sweep_tick
from src.core.datasource_setup import inject_datasource_index
from src.core.workspace import WorkspaceManager, WorkspaceManagerConfig
from src.managers.git_manager import GitManager
from src.services.knowledge.bindings import (
    NATIVE_PROJECT_CONFIG_KEY as AGENT_NATIVE_KEY,
    build_knowledge_bindings,
)
from tests._fs_backend import FilesystemTestBackend

PROJECT_ID = "1a387b4d-1111-2222-3333-444444444444"
ID8 = PROJECT_ID[:8]
OWNER_ID = "9c9c9c9c-0000-0000-0000-000000000001"
DATASOURCE_ID = uuid.UUID("55555555-6666-7777-8888-999999999999")


def native_kb_row(project_id: str = PROJECT_ID, **overrides) -> dict:
    """A project's own KB datasource, as ``create_project`` writes it."""
    row = {
        "id": DATASOURCE_ID,
        "type": "kb",
        "name": f"Better Resavio Knowledge ({project_id[:8]})",
        "connection_url": None,
        "credentials": {},
        "default_branch": None,
        "config": {
            "root_path": "knowledge",
            NATIVE_PROJECT_CONFIG_KEY: project_id,
        },
    }
    row.update(overrides)
    return row


def external_kb_row(**overrides) -> dict:
    """A genuinely external OKF KB — the kind that must still be swept."""
    row = {
        "id": uuid.uuid4(),
        "type": "kb",
        "name": "Team Docs",
        "connection_url": "https://example.test/team-docs.git",
        "credentials": {"token": "orchestrator-only"},
        "default_branch": "main",
        "config": {"root_path": "vault"},
    }
    row.update(overrides)
    return row


# =============================================================================
# Project creation — criterion 2
# =============================================================================


class TestProjectCreationProvisioning:
    def _db(self) -> MagicMock:
        db = MagicMock()
        db.create_project = AsyncMock(
            return_value={"id": PROJECT_ID, "name": "Better Resavio"}
        )
        db.add_project_member = AsyncMock()
        db.add_project_repository = AsyncMock()
        db.get_user = AsyncMock(return_value={"email": "owner@example.test"})
        db.create_datasource = AsyncMock(
            return_value={"id": DATASOURCE_ID, "name": "Better Resavio Knowledge"}
        )
        db.link_datasource_to_project = AsyncMock(return_value=True)
        return db

    def _gitea(self, **kwargs) -> MagicMock:
        gitea = MagicMock()
        gitea.is_initialized = True
        gitea.create_repo = AsyncMock(
            side_effect=lambda name: f"http://user:pw@gitea/srw/{name}.git",
            **kwargs,
        )
        gitea.grant_user_repo_access = AsyncMock()
        return gitea

    async def _create(self, db, gitea):
        with (
            patch(
                "main.require_approved_user",
                AsyncMock(return_value={"id": OWNER_ID, "is_admin": False}),
            ),
            patch("main.postgres_db", db),
            patch("main.gitea_client", gitea),
            patch(
                "main._ensure_project_cloud_resources",
                AsyncMock(side_effect=lambda project: project),
            ),
        ):
            return await create_project(
                ProjectCreate(name="Better Resavio", user_id=OWNER_ID), object()
            )

    @pytest.mark.asyncio
    async def test_creates_exactly_two_managed_repos(self):
        db, gitea = self._db(), self._gitea()

        await self._create(db, gitea)

        by_role = {
            call.kwargs["role"]: call.kwargs
            for call in db.add_project_repository.await_args_list
        }
        assert set(by_role) == {"jobs", "knowledge"}
        assert db.add_project_repository.await_count == 2
        assert by_role["jobs"]["name"] == f"project-{ID8}-jobs"
        assert by_role["knowledge"]["name"] == f"project-{ID8}-knowledge"
        assert all(kwargs["is_managed"] for kwargs in by_role.values())
        assert [c.args[0] for c in gitea.create_repo.await_args_list] == [
            f"project-{ID8}-jobs",
            f"project-{ID8}-knowledge",
        ]

    @pytest.mark.asyncio
    async def test_attaches_exactly_one_kb_datasource_marked_native(self):
        db, gitea = self._db(), self._gitea()

        await self._create(db, gitea)

        db.create_datasource.assert_awaited_once()
        kwargs = db.create_datasource.await_args.kwargs
        assert kwargs["ds_type"] == "kb"
        assert kwargs["config"] == {
            "root_path": "knowledge",
            NATIVE_PROJECT_CONFIG_KEY: PROJECT_ID,
        }
        # The vault's location lives in project_repositories and nowhere else —
        # a second copy here is how the reader and the writer drift apart, and
        # the authenticated Gitea URL would leak credentials through the API.
        assert kwargs["connection_url"] is None
        assert kwargs["created_by"] == OWNER_ID
        db.link_datasource_to_project.assert_awaited_once()
        assert db.link_datasource_to_project.await_args.args[:2] == (
            PROJECT_ID,
            str(DATASOURCE_ID),
        )

    @pytest.mark.asyncio
    async def test_datasource_name_is_unique_per_project(self):
        """(name, type, owner) is unique, so two projects sharing a title must
        not collide on their KB connectors."""
        db, gitea = self._db(), self._gitea()

        await self._create(db, gitea)

        assert ID8 in db.create_datasource.await_args.kwargs["name"]

    @pytest.mark.asyncio
    async def test_creator_gets_access_to_both_repos(self):
        db, gitea = self._db(), self._gitea()

        await self._create(db, gitea)

        granted = {c.args[1] for c in gitea.grant_user_repo_access.await_args_list}
        assert granted == {f"project-{ID8}-jobs", f"project-{ID8}-knowledge"}

    @pytest.mark.asyncio
    async def test_gitea_hiccup_does_not_fail_project_creation(self):
        """The knowledge repo is optional: resolve_kb_repo falls back to the
        jobs repo, which is what every pre-separation project already does."""
        db = self._db()
        gitea = self._gitea()
        gitea.create_repo = AsyncMock(
            side_effect=[
                f"http://gitea/srw/project-{ID8}-jobs.git",
                RuntimeError("502"),
            ]
        )

        project = await self._create(db, gitea)

        assert project["id"] == PROJECT_ID
        roles = [c.kwargs["role"] for c in db.add_project_repository.await_args_list]
        assert roles == ["jobs"]
        db.create_datasource.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refused_knowledge_repo_attaches_no_datasource(self):
        """No repo means no vault to manage — a connector pointing at nothing
        would be worse than its absence."""
        db = self._db()
        gitea = self._gitea()
        gitea.create_repo = AsyncMock(
            side_effect=[f"http://gitea/srw/project-{ID8}-jobs.git", None]
        )

        project = await self._create(db, gitea)

        assert project["id"] == PROJECT_ID
        db.create_datasource.assert_not_awaited()
        db.link_datasource_to_project.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_provisioning_is_skipped_without_gitea(self):
        db = self._db()
        gitea = self._gitea()
        gitea.is_initialized = False

        await self._create(db, gitea)

        db.add_project_repository.assert_not_awaited()
        db.create_datasource.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_helper_returns_the_created_row(self):
        db, gitea = self._db(), self._gitea()
        with patch("main.postgres_db", db), patch("main.gitea_client", gitea):
            created = await _provision_project_knowledge_repo(
                {"id": PROJECT_ID, "name": "Better Resavio"}, OWNER_ID
            )
        assert created["id"] == DATASOURCE_ID


# =============================================================================
# The external sweep — criterion 5
# =============================================================================


class TestExternalSweepSkipsNativeKb:
    def _sweep(self, rows, reindex_fn):
        postgres_db = AsyncMock()
        postgres_db.fetch.return_value = []  # no native project repos this tick
        postgres_db.list_datasources.return_value = rows
        return kb_sweep_tick(
            postgres_db=postgres_db,
            store=MagicMock(),
            gitea_client=MagicMock(),
            embedding_service=MagicMock(),
            reindex_fn=reindex_fn,
        )

    @pytest.mark.asyncio
    async def test_project_own_kb_is_not_swept_as_an_external_source(self):
        """Patching the external indexer itself, not the reindex callable it
        wraps: the sweep must not *attempt* the row. Asserting only on the
        inner callable would also pass if the attempt happened and was caught
        by the ValueError backstop, which is a swallowed error every tick, not
        a skip."""
        indexer = AsyncMock(return_value={"status": "completed"})

        with patch(
            "orchestrator.services.kb_datasources.reindex_kb_datasource", indexer
        ):
            worked = await self._sweep([native_kb_row()], AsyncMock())

        assert worked == 0
        indexer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_external_kbs_are_still_swept_alongside_a_native_one(
        self, monkeypatch
    ):
        """The skip must be surgical: a genuinely external KB in the same
        result set keeps its own index under its own datasource id."""
        monkeypatch.setenv("KB_GIT_ALLOWED_HOSTS", "example.test")
        external = external_kb_row()
        reindex_fn = AsyncMock(return_value={"status": "completed"})

        worked = await self._sweep([native_kb_row(), external], reindex_fn)

        assert worked == 1
        reindex_fn.assert_awaited_once()
        assert reindex_fn.await_args.kwargs["kb_id"] == uuid.UUID(str(external["id"]))

    @pytest.mark.asyncio
    async def test_reindexing_a_native_row_directly_is_refused(self):
        """The backstop: every external index path funnels through here, so a
        caller that forgets the skip fails loudly instead of silently
        duplicating a whole vault."""
        with pytest.raises(ValueError, match="project's own knowledge base"):
            await reindex_kb_datasource(
                native_kb_row(),
                store=MagicMock(),
                embedding_service=MagicMock(),
                is_active=AsyncMock(return_value=True),
                reindex_fn=AsyncMock(),
            )

    @pytest.mark.asyncio
    async def test_manual_reindex_endpoint_refuses_a_native_row(self):
        with (
            patch(
                "main.require_datasource_owner",
                AsyncMock(return_value=({}, native_kb_row())),
            ),
            patch("main._reindex_kb_datasource_now", AsyncMock()) as now,
            pytest.raises(HTTPException) as exc,
        ):
            await reindex_datasource_knowledge(object(), str(DATASOURCE_ID))

        assert exc.value.status_code == 400
        assert "own knowledge base" in str(exc.value.detail)
        now.assert_not_awaited()

    def test_marker_reader_ignores_rows_without_it(self):
        assert native_kb_project_id(external_kb_row()) is None
        assert native_kb_project_id({"type": "kb"}) is None
        assert native_kb_project_id({"type": "kb", "config": None}) is None
        assert native_kb_project_id(None) is None
        assert native_kb_project_id(native_kb_row()) == PROJECT_ID


# =============================================================================
# The marker survives round-trips — a stripped marker is a double index
# =============================================================================


class TestMarkerDurability:
    def test_stored_config_keeps_the_marker(self):
        stored = {"root_path": "knowledge", NATIVE_PROJECT_CONFIG_KEY: PROJECT_ID}
        assert _normalize_kb_config(stored, stored=True) == stored

    def test_user_supplied_marker_is_rejected(self):
        """Nobody can hand-forge a row out of the sweep from the outside."""
        with pytest.raises(HTTPException) as exc:
            _normalize_kb_config({NATIVE_PROJECT_CONFIG_KEY: PROJECT_ID})
        assert exc.value.status_code == 400
        assert NATIVE_PROJECT_CONFIG_KEY in str(exc.value.detail)

    @pytest.mark.asyncio
    async def test_editing_the_root_path_does_not_strip_the_marker(self):
        """Losing the marker on an ordinary edit would drop the vault back
        into the external sweep — the double index, arrived at by accident."""
        db = MagicMock()
        db.update_datasource = AsyncMock(return_value=True)
        db.list_datasource_projects = AsyncMock(return_value=[])
        schedule = MagicMock()

        with (
            patch(
                "main.require_datasource_owner",
                AsyncMock(return_value=({}, native_kb_row())),
            ),
            patch("main.postgres_db", db),
            patch("main._mark_kb_datasource_pending", AsyncMock()) as pending,
            patch("main._schedule_kb_datasource_reindex", schedule),
        ):
            await update_datasource(
                object(),
                str(DATASOURCE_ID),
                DatasourceUpdate(name="Renamed", config={"root_path": "notes"}),
            )

        written = db.update_datasource.await_args.kwargs["config"]
        assert written[NATIVE_PROJECT_CONFIG_KEY] == PROJECT_ID
        assert written["root_path"] == "notes"
        # ...and no external rebuild is ever scheduled for it
        schedule.assert_not_called()
        pending.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_native_row_update_does_not_demand_a_repository_url(self):
        """It has no remote to validate; requiring one would make renaming the
        project's own connector a 400."""
        db = MagicMock()
        db.update_datasource = AsyncMock(return_value=True)
        db.list_datasource_projects = AsyncMock(return_value=[])

        with (
            patch(
                "main.require_datasource_owner",
                AsyncMock(return_value=({}, native_kb_row())),
            ),
            patch("main.postgres_db", db),
            patch("main._schedule_kb_datasource_reindex", MagicMock()),
        ):
            result = await update_datasource(
                object(), str(DATASOURCE_ID), DatasourceUpdate(name="Renamed")
            )

        assert result == {"status": "updated"}
        assert db.update_datasource.await_args.kwargs["connection_url"] is None


# =============================================================================
# Runtime bindings — one KB, one binding, still writable
# =============================================================================


class TestBindingsDoNotDuplicateTheProjectKb:
    def test_own_kb_datasource_collapses_into_the_writable_native_binding(self):
        bindings = build_knowledge_bindings(
            project_ids=[PROJECT_ID],
            datasources=[native_kb_row()],
        )

        assert len(bindings) == 1
        binding = bindings[0]
        assert binding.kb_id == uuid.UUID(PROJECT_ID)
        assert binding.kind == "native"
        assert binding.writable is True
        assert binding.alias == "project"

    def test_no_second_alias_for_the_same_kb_id(self):
        bindings = build_knowledge_bindings(
            project_ids=[PROJECT_ID],
            datasources=[native_kb_row(), external_kb_row()],
        )

        assert len(bindings) == 2
        assert len({b.kb_id for b in bindings}) == 2
        assert [b.kind for b in bindings] == ["native", "datasource"]
        assert uuid.UUID(PROJECT_ID) not in {
            b.kb_id for b in bindings if b.kind == "datasource"
        }

    def test_external_kbs_stay_bound_and_read_only(self):
        external = external_kb_row()

        bindings = build_knowledge_bindings(
            project_ids=[PROJECT_ID], datasources=[external]
        )

        docs = next(b for b in bindings if b.kind == "datasource")
        assert docs.kb_id == uuid.UUID(str(external["id"]))
        assert docs.writable is False
        assert docs.root_path == "vault"

    def test_native_row_without_its_project_binds_by_project_id(self):
        """Selected for a job in another project: still one KB, still keyed by
        the id its notes are actually indexed under, and read-only there."""
        bindings = build_knowledge_bindings(
            project_ids=[], datasources=[native_kb_row()]
        )

        assert len(bindings) == 1
        assert bindings[0].kb_id == uuid.UUID(PROJECT_ID)
        assert bindings[0].writable is False

    def test_marker_constant_matches_the_orchestrator(self):
        """The agent image cannot import orchestrator code, so the constant is
        duplicated — a silent rename on one side is a silent double index."""
        assert AGENT_NATIVE_KEY == NATIVE_PROJECT_CONFIG_KEY

    def test_connector_index_does_not_advertise_the_project_kb_as_read_only(self):
        ws = MagicMock()
        ws.read_file.return_value = ""

        inject_datasource_index([native_kb_row(), external_kb_row()], ws)

        written = ws.write_file.call_args.args[1]
        assert "Team Docs" in written
        assert "Better Resavio Knowledge" not in written


# =============================================================================
# The vault is never checked out — criterion 6
# =============================================================================


class TestKnowledgeRepoIsNotCloned:
    def _workspace(self, tmp_path: Path) -> WorkspaceManager:
        return WorkspaceManager(
            job_id="test-job",
            config=WorkspaceManagerConfig(
                structure=["archive/"],
                git_versioning=True,
                repositories=[
                    {
                        "role": "jobs",
                        "name": f"project-{ID8}-jobs",
                        "repo_url": "http://gitea/srw/jobs.git",
                    },
                    {
                        "role": "knowledge",
                        "name": f"project-{ID8}-knowledge",
                        "repo_url": "http://gitea/srw/knowledge.git",
                    },
                    {
                        "role": "source",
                        "name": "kurort-engine",
                        "repo_url": "http://gitea/srw/kurort.git",
                    },
                ],
            ),
            base_path=tmp_path,
            backend=FilesystemTestBackend(tmp_path),
        )

    def test_only_source_repos_land_in_repos(self, tmp_path, monkeypatch):
        cloned: list[str] = []

        def _record(repo_url, target, **kwargs):
            cloned.append(Path(target).name)
            return None  # clone "failed" — the call is what this test is about

        monkeypatch.setattr(GitManager, "clone", staticmethod(_record))

        self._workspace(tmp_path)._clone_auxiliary_repos()

        assert cloned == ["kurort-engine"]
        assert f"project-{ID8}-knowledge" not in cloned
        assert not (tmp_path / "repos" / f"project-{ID8}-knowledge").exists()
