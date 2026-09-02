"""KB prefilter lane (kb_gardening G7 rule R4) — deterministic, staged
retirement of nursery notes no active durable note reaches.

Pure unit: store + metadata materializer mocked. `orchestrator/services/kb_prefilter.py`.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.kb_prefilter import (
    NURSERY_TYPES,
    PROTECTED_TAGS,
    ROOT_TYPES,
    prefilter_enabled,
    prefilter_kb_tick,
    prefilter_max_per_tick,
    prefilter_min_age,
)
from services.kb_reindex import kb_sweep_tick

KB = uuid.uuid4()


def _row(slug, note_type="learning"):
    return {
        "note_id": slug,
        "path": f"knowledge/{slug}.md",
        "blob_sha": "a" * 40,
        "note_type": note_type,
        "title": slug,
    }


def _store(candidates):
    store = MagicMock()
    store.list_unreachable_nursery = AsyncMock(return_value=candidates)
    store.set_note_status = AsyncMock(return_value=True)
    return store


class TestKnobs:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv("KB_PREFILTER_ENABLED", raising=False)
        assert prefilter_enabled() is False

    def test_defaults(self, monkeypatch):
        for name in ("KB_PREFILTER_MIN_AGE_DAYS", "KB_PREFILTER_MAX_PER_TICK"):
            monkeypatch.delenv(name, raising=False)
        assert prefilter_min_age() == timedelta(days=7)
        assert prefilter_max_per_tick() == 25

    def test_vocabularies_match_the_design(self):
        assert set(NURSERY_TYPES) == {"learning", "retrospective", "state"}
        assert {
            "decision",
            "goal",
            "plan",
            "charter",
            "feature",
            "issue",
            "idea",
        } <= set(ROOT_TYPES)
        assert set(PROTECTED_TAGS) == {"pinned", "ready", "parallel-safe"}
        # a root type is never a nursery type and vice versa
        assert not set(ROOT_TYPES) & set(NURSERY_TYPES)


class TestPrefilterTick:
    @pytest.mark.asyncio
    async def test_enumerates_with_the_rule_vocabulary_and_cap(self):
        store = _store([])
        with patch("services.kb_prefilter.materialize_knowledge_metadata_update") as m:
            counts = await prefilter_kb_tick(
                postgres_db="db",
                store=store,
                gitea_client="g",
                kb_id=KB,
                min_age=timedelta(days=3),
                limit=4,
            )
        kwargs = store.list_unreachable_nursery.await_args.kwargs
        assert store.list_unreachable_nursery.await_args.args[0] == KB
        assert set(kwargs["root_types"]) == set(ROOT_TYPES)
        assert set(kwargs["nursery_types"]) == set(NURSERY_TYPES)
        assert set(kwargs["protected_tags"]) == set(PROTECTED_TAGS)
        assert kwargs["min_age"] == timedelta(days=3)
        assert kwargs["limit"] == 4
        m.assert_not_called()
        assert counts == {"candidates": 0, "archived": 0, "unchanged": 0, "failed": 0}

    @pytest.mark.asyncio
    async def test_archives_file_first_then_flips_the_row_with_invalidated_at(self):
        store = _store([_row("old-learning"), _row("old-retro", "retrospective")])
        update = AsyncMock(
            return_value={"status": "committed", "canonical_state": "canonical"}
        )
        with patch(
            "services.kb_prefilter.materialize_knowledge_metadata_update", update
        ):
            counts = await prefilter_kb_tick(
                postgres_db="db", store=store, gitea_client="g", kb_id=KB
            )
        assert counts["archived"] == 2
        first = update.await_args_list[0].kwargs
        assert first == {
            "postgres_db": "db",
            "gitea_client": "g",
            "project_id": str(KB),
            "slug": "old-learning",
            "status": "archived",
        }
        flips = [
            c.args + tuple(sorted(c.kwargs.items()))
            for c in store.set_note_status.await_args_list
        ]
        assert flips[0] == (KB, "old-learning", "archived", ("invalidated", True))
        assert flips[1][1] == "old-retro"

    @pytest.mark.asyncio
    async def test_file_already_archived_counts_as_unchanged_but_still_flips_row(self):
        store = _store([_row("x")])
        update = AsyncMock(
            return_value={
                "status": "skipped",
                "reason": "unchanged",
                "canonical_state": "canonical",
            }
        )
        with patch(
            "services.kb_prefilter.materialize_knowledge_metadata_update", update
        ):
            counts = await prefilter_kb_tick(
                postgres_db="db", store=store, gitea_client="g", kb_id=KB
            )
        assert counts == {"candidates": 1, "archived": 0, "unchanged": 1, "failed": 0}
        store.set_note_status.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_failed_file_rewrite_never_flips_the_row(self):
        """The file is the truth; a row-only archive would be reverted by the
        next sweep and would lie to search in the meantime."""
        store = _store([_row("x")])
        update = AsyncMock(
            return_value={
                "status": "failed",
                "reason": "commit-error",
                "canonical_state": "pending_sync",
            }
        )
        with patch(
            "services.kb_prefilter.materialize_knowledge_metadata_update", update
        ):
            counts = await prefilter_kb_tick(
                postgres_db="db", store=store, gitea_client="g", kb_id=KB
            )
        assert counts["failed"] == 1 and counts["archived"] == 0
        store.set_note_status.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exceptions_are_counted_not_raised(self):
        store = _store([_row("x"), _row("y")])
        update = AsyncMock(side_effect=[RuntimeError("boom"), {"status": "committed"}])
        with patch(
            "services.kb_prefilter.materialize_knowledge_metadata_update", update
        ):
            counts = await prefilter_kb_tick(
                postgres_db="db", store=store, gitea_client="g", kb_id=KB
            )
        assert counts == {"candidates": 2, "archived": 1, "unchanged": 0, "failed": 1}

    @pytest.mark.asyncio
    async def test_enumeration_failure_is_a_no_op(self):
        store = MagicMock()
        store.list_unreachable_nursery = AsyncMock(side_effect=RuntimeError("db"))
        update = AsyncMock()
        with patch(
            "services.kb_prefilter.materialize_knowledge_metadata_update", update
        ):
            counts = await prefilter_kb_tick(
                postgres_db="db", store=store, gitea_client="g", kb_id=KB
            )
        assert counts["candidates"] == 0
        update.assert_not_awaited()


class TestSweepIntegration:
    @staticmethod
    def _db(project_id):
        db = AsyncMock()
        db.claim_due_knowledge_materializations.return_value = []
        db.list_datasources.return_value = []
        db.fetch.return_value = [{"project_id": project_id}]
        db.mark_knowledge_projections_synced.return_value = 0
        return db

    @pytest.mark.asyncio
    async def test_prefilter_runs_before_purge_when_enabled(self):
        project_id = uuid.uuid4()
        order = []

        async def reindex(**kwargs):
            order.append("reindex")
            return {"status": "completed"}

        async def prefilter(**kwargs):
            order.append("prefilter")
            return {"archived": 2}

        async def purge(**kwargs):
            order.append("purge")
            return {"purged": 0}

        with patch(
            "services.kb_reindex.resolve_kb_repo",
            AsyncMock(return_value=MagicMock(repo="r", branch="main", forge="gitea")),
        ):
            n = await kb_sweep_tick(
                postgres_db=self._db(project_id),
                store=MagicMock(),
                gitea_client=MagicMock(),
                embedding_service=MagicMock(),
                reindex_fn=reindex,
                prefilter_fn=prefilter,
                prefilter_enabled_fn=lambda: True,
                purge_fn=purge,
                purge_enabled_fn=lambda: True,
            )
        assert order == ["reindex", "prefilter", "purge"]
        assert n == 2  # completed reindex + a prefilter that archived something

    @pytest.mark.asyncio
    async def test_prefilter_is_skipped_when_disabled(self):
        project_id = uuid.uuid4()
        prefilter = AsyncMock(return_value={"archived": 0})
        with patch(
            "services.kb_reindex.resolve_kb_repo",
            AsyncMock(return_value=MagicMock(repo="r", branch="main", forge="gitea")),
        ):
            await kb_sweep_tick(
                postgres_db=self._db(project_id),
                store=MagicMock(),
                gitea_client=MagicMock(),
                embedding_service=MagicMock(),
                reindex_fn=AsyncMock(return_value={"status": "up-to-date"}),
                prefilter_fn=prefilter,
                prefilter_enabled_fn=lambda: False,
                purge_enabled_fn=lambda: False,
            )
        prefilter.assert_not_awaited()
