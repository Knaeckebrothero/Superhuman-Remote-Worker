"""Tests for OKF KB slice 3 PR3 — tree-diff reindexer.

Design: docs/features/okf_knowledge_base.md §5 / §5.1 / §11 slice-3 PR3.

The reindexer is the composition PR: watermark → git tree diff → parse changed
notes (gardener parse_note_md) → chunk+embed (PR2) → persist (PR1 store surface)
→ advance watermark. Pure helpers first (blob-map filter, diff plan, frontmatter
field mapping), then the orchestration with AsyncMock deps.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.knowledge_store import KbWatermark
from src.tools.knowledge.chunker import CHUNKER_VERSION

from orchestrator.services.kb_reindex import (
    kb_sweep_tick,
    knowledge_blob_map,
    note_fields,
    plan_reindex,
    reindex_kb,
    resolve_kb_repo,
)

CURRENT_VERSION = f"qwen3-embedding-8b:4096:{CHUNKER_VERSION}"


def _note_md(slug: str, body: str = "the body", note_type: str = "learning") -> str:
    return f"---\nid: {slug}\ntype: {note_type}\nstatus: active\n---\n# {slug}\n\n{body}\n"


def _make_deps(
    *,
    head="headsha",
    watermark=None,
    tree=None,
    indexed=None,
    contents=None,
):
    """AsyncMock gitea + store + embedding service for reindex_kb."""
    gitea = AsyncMock()
    gitea.get_branch_head_sha.return_value = head
    gitea.list_tree.return_value = tree if tree is not None else []
    contents = contents or {}
    gitea.get_file_content.side_effect = lambda repo, path, ref=None: contents.get(path)

    store = AsyncMock()
    store.get_watermark.return_value = watermark
    store.get_indexed_blob_shas.return_value = indexed or {}
    store.adopt_legacy_row.return_value = None
    store.upsert_kb_note.return_value = uuid.uuid4()
    store.replace_note_chunks.return_value = 1
    store.delete_kb_note.return_value = True

    svc = MagicMock()
    svc.model = "qwen3-embedding-8b"
    svc.expected_dimensions = 4096

    async def _batch(texts):
        return [[0.1] for _ in texts]

    svc.embed_batch = AsyncMock(side_effect=_batch)
    return gitea, store, svc


# =============================================================================
# knowledge_blob_map — filter a gitea list_tree to indexable knowledge notes
# =============================================================================


class TestKnowledgeBlobMap:
    def test_filters_to_knowledge_md_blobs(self):
        tree = [
            {"path": "knowledge/chose-jwt.md", "type": "blob", "sha": "a1"},
            {"path": "knowledge/deep/nested.md", "type": "blob", "sha": "a2"},
            {"path": "knowledge", "type": "tree", "sha": "t1"},
            {"path": "projects/x/report.md", "type": "blob", "sha": "a3"},
            {"path": "README.md", "type": "blob", "sha": "a4"},
            {"path": "knowledge/diagram.png", "type": "blob", "sha": "a5"},
        ]
        assert knowledge_blob_map(tree) == {
            "knowledge/chose-jwt.md": "a1",
            "knowledge/deep/nested.md": "a2",
        }

    def test_skips_reserved_index_and_log(self):
        tree = [
            {"path": "knowledge/index.md", "type": "blob", "sha": "a1"},
            {"path": "knowledge/log.md", "type": "blob", "sha": "a2"},
            {"path": "knowledge/real-note.md", "type": "blob", "sha": "a3"},
        ]
        assert knowledge_blob_map(tree) == {"knowledge/real-note.md": "a3"}

    def test_empty_tree(self):
        assert knowledge_blob_map([]) == {}


# =============================================================================
# plan_reindex — set-diff with per-row blob_sha self-heal
# =============================================================================


class TestPlanReindex:
    def test_changed_added_deleted(self):
        indexed = {"knowledge/a.md": "sha1", "knowledge/b.md": "sha2", "knowledge/c.md": "sha3"}
        current = {"knowledge/a.md": "sha1", "knowledge/b.md": "CHANGED", "knowledge/d.md": "NEW"}
        upserts, deletes = plan_reindex(indexed, current)
        assert upserts == ["knowledge/b.md", "knowledge/d.md"]  # sorted
        assert deletes == ["knowledge/c.md"]

    def test_unchanged_sha_is_skipped_self_heal(self):
        # An interrupted reindex left this row current — the re-run skips it.
        indexed = {"knowledge/a.md": "same"}
        current = {"knowledge/a.md": "same"}
        upserts, deletes = plan_reindex(indexed, current)
        assert upserts == []
        assert deletes == []

    def test_full_rebuild_upserts_everything(self):
        indexed = {"knowledge/a.md": "same", "knowledge/gone.md": "x"}
        current = {"knowledge/a.md": "same", "knowledge/b.md": "n"}
        upserts, deletes = plan_reindex(indexed, current, full=True)
        assert upserts == ["knowledge/a.md", "knowledge/b.md"]
        assert deletes == ["knowledge/gone.md"]

    def test_empty_current_deletes_all(self):
        upserts, deletes = plan_reindex({"knowledge/a.md": "x"}, {})
        assert upserts == []
        assert deletes == ["knowledge/a.md"]


# =============================================================================
# note_fields — invert _render_note_md's frontmatter into upsert_kb_note args
# =============================================================================


class TestNoteFields:
    def test_maps_full_frontmatter(self):
        fm = {
            "id": "chose-jwt",
            "type": "decision",
            "description": "Why JWT",
            "tags": ["auth", "security"],
            "keywords": ["JWT"],
            "confidence": "high",
            "status": "superseded",
            "superseded_by": "chose-paseto",
        }
        body = "# Chose JWT over OAuth\n\nBecause stateless."
        f = note_fields("knowledge/chose-jwt.md", fm, body)
        assert f["note_id"] == "chose-jwt"
        assert f["title"] == "Chose JWT over OAuth"
        assert f["note_type"] == "decision"
        assert f["status"] == "superseded"
        assert f["tags"] == ["auth", "security"]
        assert f["keywords"] == ["JWT"]
        assert f["confidence"] == "high"
        assert f["superseded_by"] == "chose-paseto"

    def test_no_frontmatter_derives_from_path_and_body(self):
        # Human-authored note with no frontmatter: id from the filename stem,
        # title from the first H1, safe defaults everywhere else.
        f = note_fields("knowledge/my-note.md", None, "# My Note\n\nbody")
        assert f["note_id"] == "my-note"
        assert f["title"] == "My Note"
        assert f["note_type"] == "learning"  # CHECK-constraint-safe default
        assert f["status"] == "active"
        assert f["tags"] == []
        assert f["superseded_by"] is None

    def test_invalid_type_and_status_fall_back_safely(self):
        # valid_note_type / valid_note_status CHECK constraints would reject
        # arbitrary frontmatter values — map unknowns to safe defaults.
        fm = {"id": "n", "type": "musing", "status": "kinda-done"}
        f = note_fields("knowledge/n.md", fm, "body")
        assert f["note_type"] == "learning"
        assert f["status"] == "active"

    def test_title_falls_back_to_note_id_without_h1(self):
        f = note_fields("knowledge/n.md", {"id": "n", "type": "code"}, "no heading")
        assert f["title"] == "n"

    def test_note_id_truncated_to_column_limit(self):
        long_id = "x" * 150
        f = note_fields("knowledge/n.md", {"id": long_id, "type": "code"}, "b")
        assert len(f["note_id"]) == 100  # VARCHAR(100)

    def test_scalar_tags_normalized_to_list(self):
        fm = {"id": "n", "type": "code", "tags": "single-tag"}
        f = note_fields("knowledge/n.md", fm, "b")
        assert f["tags"] == ["single-tag"]


# =============================================================================
# reindex_kb — the orchestration
# =============================================================================


class TestReindexKbShortCircuits:
    @pytest.mark.asyncio
    async def test_no_head_returns_without_work(self):
        gitea, store, svc = _make_deps(head=None)
        result = await reindex_kb(
            gitea_client=gitea, store=store, embedding_service=svc,
            kb_id=uuid.uuid4(), repo_name="r",
        )
        assert result["status"] == "no-head"
        gitea.list_tree.assert_not_awaited()
        store.upsert_watermark.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_up_to_date_skips_tree_fetch(self):
        kb = uuid.uuid4()
        wm = KbWatermark(
            kb_id=kb, indexed_commit="headsha", pipeline_version=CURRENT_VERSION
        )
        gitea, store, svc = _make_deps(head="headsha", watermark=wm)
        result = await reindex_kb(
            gitea_client=gitea, store=store, embedding_service=svc,
            kb_id=kb, repo_name="r",
        )
        assert result["status"] == "up-to-date"
        gitea.list_tree.assert_not_awaited()
        store.upsert_watermark.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_force_full_bypasses_up_to_date(self):
        kb = uuid.uuid4()
        wm = KbWatermark(
            kb_id=kb, indexed_commit="headsha", pipeline_version=CURRENT_VERSION
        )
        gitea, store, svc = _make_deps(head="headsha", watermark=wm, tree=[])
        result = await reindex_kb(
            gitea_client=gitea, store=store, embedding_service=svc,
            kb_id=kb, repo_name="r", force_full=True,
        )
        assert result["status"] == "completed"
        gitea.list_tree.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tree_fetch_failure_does_not_advance_watermark(self):
        gitea, store, svc = _make_deps(head="headsha")
        gitea.list_tree.return_value = None
        result = await reindex_kb(
            gitea_client=gitea, store=store, embedding_service=svc,
            kb_id=uuid.uuid4(), repo_name="r",
        )
        assert result["status"] == "tree-fetch-failed"
        store.upsert_watermark.assert_not_awaited()


class TestReindexKbIncremental:
    def _tree(self):
        return [
            {"path": "knowledge/changed.md", "type": "blob", "sha": "NEW"},
            {"path": "knowledge/same.md", "type": "blob", "sha": "same1"},
        ]

    @pytest.mark.asyncio
    async def test_changed_note_flows_through_pipeline(self):
        kb = uuid.uuid4()
        wm = KbWatermark(
            kb_id=kb, indexed_commit="old", pipeline_version=CURRENT_VERSION
        )
        gitea, store, svc = _make_deps(
            head="headsha",
            watermark=wm,
            tree=self._tree(),
            indexed={"knowledge/changed.md": "OLD", "knowledge/same.md": "same1"},
            contents={"knowledge/changed.md": _note_md("changed", "fresh insight")},
        )
        result = await reindex_kb(
            gitea_client=gitea, store=store, embedding_service=svc,
            kb_id=kb, repo_name="r",
        )
        assert result["status"] == "completed"
        assert result["upserted"] == 1
        # unchanged sha skipped — only the changed note was fetched
        gitea.get_file_content.assert_awaited_once_with(
            "r", "knowledge/changed.md", ref="headsha"
        )
        # adopt-then-upsert ordering for the legacy-collision guard
        store.adopt_legacy_row.assert_awaited_once_with(
            kb, "changed", "knowledge/changed.md"
        )
        up_kwargs = store.upsert_kb_note.await_args[1]
        assert up_kwargs["kb_id"] == kb
        assert up_kwargs["path"] == "knowledge/changed.md"
        assert up_kwargs["blob_sha"] == "NEW"
        assert up_kwargs["embedding_version"] == CURRENT_VERSION
        # chunks persisted with the note row id
        rc_kwargs = store.replace_note_chunks.await_args[1]
        assert rc_kwargs["kb_id"] == kb
        assert rc_kwargs["embedding_version"] == CURRENT_VERSION
        assert len(rc_kwargs["chunks"]) >= 1
        # watermark advanced to head with the current pipeline version
        wm_kwargs = store.upsert_watermark.await_args[1]
        assert wm_kwargs["indexed_commit"] == "headsha"
        assert wm_kwargs["pipeline_version"] == CURRENT_VERSION

    @pytest.mark.asyncio
    async def test_deleted_note_removed_from_index(self):
        kb = uuid.uuid4()
        wm = KbWatermark(
            kb_id=kb, indexed_commit="old", pipeline_version=CURRENT_VERSION
        )
        gitea, store, svc = _make_deps(
            head="headsha",
            watermark=wm,
            tree=[{"path": "knowledge/same.md", "type": "blob", "sha": "same1"}],
            indexed={"knowledge/same.md": "same1", "knowledge/gone.md": "x"},
        )
        result = await reindex_kb(
            gitea_client=gitea, store=store, embedding_service=svc,
            kb_id=kb, repo_name="r",
        )
        assert result["status"] == "completed"
        assert result["deleted"] == 1
        store.delete_kb_note.assert_awaited_once_with(kb, "knowledge/gone.md")

    @pytest.mark.asyncio
    async def test_pipeline_version_change_forces_full_rebuild(self):
        kb = uuid.uuid4()
        wm = KbWatermark(
            kb_id=kb, indexed_commit="old", pipeline_version="old-model:1024:c0"
        )
        gitea, store, svc = _make_deps(
            head="headsha",
            watermark=wm,
            tree=self._tree(),
            indexed={"knowledge/changed.md": "NEW", "knowledge/same.md": "same1"},
            contents={
                "knowledge/changed.md": _note_md("changed"),
                "knowledge/same.md": _note_md("same"),
            },
        )
        result = await reindex_kb(
            gitea_client=gitea, store=store, embedding_service=svc,
            kb_id=kb, repo_name="r",
        )
        # matching blob_shas notwithstanding, EVERY note re-embeds
        assert result["upserted"] == 2
        assert result["full"] is True

    @pytest.mark.asyncio
    async def test_no_watermark_means_full_rebuild(self):
        gitea, store, svc = _make_deps(
            head="headsha",
            watermark=None,
            tree=[{"path": "knowledge/a.md", "type": "blob", "sha": "s"}],
            contents={"knowledge/a.md": _note_md("a")},
        )
        result = await reindex_kb(
            gitea_client=gitea, store=store, embedding_service=svc,
            kb_id=uuid.uuid4(), repo_name="r",
        )
        assert result["full"] is True
        assert result["upserted"] == 1


class TestReindexKbFailureHonesty:
    @pytest.mark.asyncio
    async def test_fetch_failure_counts_error_and_blocks_watermark(self):
        gitea, store, svc = _make_deps(
            head="headsha",
            tree=[{"path": "knowledge/a.md", "type": "blob", "sha": "s"}],
            contents={},  # get_file_content returns None
        )
        result = await reindex_kb(
            gitea_client=gitea, store=store, embedding_service=svc,
            kb_id=uuid.uuid4(), repo_name="r",
        )
        assert result["status"] == "partial"
        assert result["errors"] == 1
        store.upsert_watermark.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unparseable_note_is_skipped_not_fatal(self):
        gitea, store, svc = _make_deps(
            head="headsha",
            tree=[
                {"path": "knowledge/bad.md", "type": "blob", "sha": "s1"},
                {"path": "knowledge/good.md", "type": "blob", "sha": "s2"},
            ],
            contents={
                "knowledge/bad.md": "---\n: bad: [yaml\n---\nbody",
                "knowledge/good.md": _note_md("good"),
            },
        )
        result = await reindex_kb(
            gitea_client=gitea, store=store, embedding_service=svc,
            kb_id=uuid.uuid4(), repo_name="r",
        )
        # the malformed note is lint's problem, not the reindexer's — the good
        # note lands and the watermark advances
        assert result["status"] == "completed"
        assert result["skipped"] == 1
        assert result["upserted"] == 1
        store.upsert_watermark.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_embed_failure_counts_error_and_blocks_watermark(self):
        gitea, store, svc = _make_deps(
            head="headsha",
            tree=[{"path": "knowledge/a.md", "type": "blob", "sha": "s"}],
            contents={"knowledge/a.md": _note_md("a")},
        )
        svc.embed_batch = AsyncMock(side_effect=RuntimeError("provider down"))
        result = await reindex_kb(
            gitea_client=gitea, store=store, embedding_service=svc,
            kb_id=uuid.uuid4(), repo_name="r",
        )
        assert result["status"] == "partial"
        assert result["errors"] == 1
        store.upsert_kb_note.assert_not_awaited()  # embed-first ordering
        store.upsert_watermark.assert_not_awaited()


# =============================================================================
# resolve_kb_repo — project → (repo_name, branch) for the KB vault
# =============================================================================


class TestResolveKbRepo:
    @pytest.mark.asyncio
    async def test_first_jobs_role_repo_wins(self):
        db = AsyncMock()
        db.get_project_repositories.return_value = [
            {"name": "project-abc-jobs", "branch": "main"},
            {"name": "project-abc-jobs-2", "branch": "dev"},
        ]
        result = await resolve_kb_repo(db, "proj-id")
        assert result == ("project-abc-jobs", "main")
        db.get_project_repositories.assert_awaited_once_with("proj-id", role="jobs")

    @pytest.mark.asyncio
    async def test_missing_branch_defaults_to_main(self):
        db = AsyncMock()
        db.get_project_repositories.return_value = [
            {"name": "project-abc-jobs", "branch": None}
        ]
        assert await resolve_kb_repo(db, "p") == ("project-abc-jobs", "main")

    @pytest.mark.asyncio
    async def test_no_repos_returns_none(self):
        db = AsyncMock()
        db.get_project_repositories.return_value = []
        assert await resolve_kb_repo(db, "p") is None


# =============================================================================
# kb_sweep_tick — one sweep over every project KB (leader-gated caller)
# =============================================================================


class TestKbSweepTick:
    def _rows(self):
        self.p1, self.p2 = uuid.uuid4(), uuid.uuid4()
        return [
            {"project_id": self.p1, "name": "project-1-jobs", "branch": "main"},
            {"project_id": self.p2, "name": "project-2-jobs", "branch": "main"},
        ]

    @pytest.mark.asyncio
    async def test_reindexes_every_jobs_repo(self):
        postgres_db = AsyncMock()
        postgres_db.fetch.return_value = self._rows()
        reindex_fn = AsyncMock(return_value={"status": "completed", "upserted": 1})
        n = await kb_sweep_tick(
            postgres_db=postgres_db,
            store=MagicMock(),
            gitea_client=MagicMock(),
            embedding_service=MagicMock(),
            reindex_fn=reindex_fn,
        )
        assert n == 2
        assert reindex_fn.await_count == 2
        kwargs = reindex_fn.await_args_list[0][1]
        assert kwargs["kb_id"] == self.p1
        assert kwargs["repo_name"] == "project-1-jobs"
        assert kwargs["branch"] == "main"
        # the work-list query targets jobs-role repos
        query = postgres_db.fetch.call_args[0][0]
        assert "project_repositories" in query
        assert "jobs" in str(postgres_db.fetch.call_args[0])

    @pytest.mark.asyncio
    async def test_up_to_date_not_counted_as_work(self):
        postgres_db = AsyncMock()
        postgres_db.fetch.return_value = self._rows()
        reindex_fn = AsyncMock(return_value={"status": "up-to-date"})
        n = await kb_sweep_tick(
            postgres_db=postgres_db,
            store=MagicMock(),
            gitea_client=MagicMock(),
            embedding_service=MagicMock(),
            reindex_fn=reindex_fn,
        )
        assert n == 0
        assert reindex_fn.await_count == 2  # still checked both

    @pytest.mark.asyncio
    async def test_one_kb_failure_does_not_kill_the_tick(self):
        postgres_db = AsyncMock()
        postgres_db.fetch.return_value = self._rows()
        reindex_fn = AsyncMock(
            side_effect=[RuntimeError("boom"), {"status": "completed"}]
        )
        n = await kb_sweep_tick(
            postgres_db=postgres_db,
            store=MagicMock(),
            gitea_client=MagicMock(),
            embedding_service=MagicMock(),
            reindex_fn=reindex_fn,
        )
        assert n == 1  # the second KB still reindexed
        assert reindex_fn.await_count == 2
