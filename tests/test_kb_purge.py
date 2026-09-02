"""KB purge lane (kb_gardening G2) — the deterministic second phase of a
retirement: long-retired, unreferenced notes get a file-removal commit.

Pure unit: the store and the materialize delete op are mocked; the lane's
own logic (gates, per-outcome counting, CAS token, bounded blast radius) is
what is under test. `orchestrator/services/kb_purge.py`.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.kb_purge import (
    PURGE_EXCLUDED_TYPES,
    purge_enabled,
    purge_grace,
    purge_kb_tick,
    purge_max_per_tick,
)
from services.kb_reindex import kb_sweep_tick

KB = uuid.uuid4()


def _row(slug: str, sha: str = "a" * 40, status: str = "archived") -> dict:
    return {
        "note_id": slug,
        "path": f"knowledge/{slug}.md",
        "blob_sha": sha,
        "status": status,
        "note_type": "learning",
    }


def _store(candidates):
    store = MagicMock()
    store.list_purge_candidates = AsyncMock(return_value=candidates)
    return store


class TestKnobs:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv("KB_PURGE_ENABLED", raising=False)
        assert purge_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
    def test_truthy_values(self, monkeypatch, value):
        monkeypatch.setenv("KB_PURGE_ENABLED", value)
        assert purge_enabled() is True

    def test_grace_defaults_to_two_weeks_and_never_below_a_day(self, monkeypatch):
        monkeypatch.delenv("KB_PURGE_GRACE_DAYS", raising=False)
        assert purge_grace() == timedelta(days=14)
        monkeypatch.setenv("KB_PURGE_GRACE_DAYS", "0")
        assert purge_grace() == timedelta(days=1)
        monkeypatch.setenv("KB_PURGE_GRACE_DAYS", "junk")
        assert purge_grace() == timedelta(days=14)

    def test_per_tick_cap(self, monkeypatch):
        monkeypatch.delenv("KB_PURGE_MAX_PER_TICK", raising=False)
        assert purge_max_per_tick() == 25
        monkeypatch.setenv("KB_PURGE_MAX_PER_TICK", "3")
        assert purge_max_per_tick() == 3

    def test_excluded_types_cover_identity_and_pipeline_history(self):
        assert {"charter", "feature", "issue", "idea", "report"} <= set(
            PURGE_EXCLUDED_TYPES
        )


class TestPurgeTick:
    @pytest.mark.asyncio
    async def test_enumerates_with_the_three_signal_rule_and_the_cap(self):
        store = _store([])
        with patch("services.kb_purge.materialize_knowledge_note_delete") as delete:
            counts = await purge_kb_tick(
                postgres_db=MagicMock(),
                store=store,
                gitea_client=MagicMock(),
                kb_id=KB,
                grace=timedelta(days=7),
                limit=5,
            )
        store.list_purge_candidates.assert_awaited_once()
        kwargs = store.list_purge_candidates.await_args.kwargs
        assert store.list_purge_candidates.await_args.args[0] == KB
        assert kwargs["grace"] == timedelta(days=7)
        assert kwargs["limit"] == 5
        assert set(kwargs["excluded_types"]) == set(PURGE_EXCLUDED_TYPES)
        delete.assert_not_called()
        assert counts == {
            "candidates": 0,
            "purged": 0,
            "absent": 0,
            "refused": 0,
            "failed": 0,
        }

    @pytest.mark.asyncio
    async def test_each_candidate_is_deleted_with_its_cas_token_and_a_reason(self):
        store = _store([_row("old-a", "1" * 40), _row("old-b", "2" * 40, "superseded")])
        delete = AsyncMock(return_value={"status": "committed", "reason": None})
        with patch("services.kb_purge.materialize_knowledge_note_delete", delete):
            counts = await purge_kb_tick(
                postgres_db="db",
                store=store,
                gitea_client="gitea",
                kb_id=KB,
                grace=timedelta(days=14),
                limit=25,
            )
        assert counts["purged"] == 2
        first = delete.await_args_list[0].kwargs
        assert first["project_id"] == str(KB)
        assert first["slug"] == "old-a"
        assert first["expected_blob_sha"] == "1" * 40
        assert first["store"] is store
        assert "14d" in first["reason"] and "archived" in first["reason"]
        assert delete.await_args_list[1].kwargs["expected_blob_sha"] == "2" * 40

    @pytest.mark.asyncio
    async def test_outcomes_are_counted_not_raised(self):
        store = _store([_row(s) for s in ("a", "b", "c", "d", "e")])
        outcomes = [
            {"status": "committed"},
            {"status": "skipped", "reason": "absent"},
            {"status": "failed", "reason": "precondition-failed"},
            {"status": "failed", "reason": "commit-error"},
            RuntimeError("boom"),
        ]
        delete = AsyncMock(side_effect=outcomes)
        with patch("services.kb_purge.materialize_knowledge_note_delete", delete):
            counts = await purge_kb_tick(
                postgres_db="db", store=store, gitea_client="g", kb_id=KB
            )
        assert counts == {
            "candidates": 5,
            "purged": 1,
            "absent": 1,
            "refused": 1,
            "failed": 2,
        }

    @pytest.mark.asyncio
    async def test_enumeration_failure_is_a_no_op(self):
        store = MagicMock()
        store.list_purge_candidates = AsyncMock(side_effect=RuntimeError("db down"))
        delete = AsyncMock()
        with patch("services.kb_purge.materialize_knowledge_note_delete", delete):
            counts = await purge_kb_tick(
                postgres_db="db", store=store, gitea_client="g", kb_id=KB
            )
        assert counts["candidates"] == 0
        delete.assert_not_awaited()


class TestSweepIntegration:
    """The lane rides the reindex sweep, after a KB's index is current, and
    only when enabled."""

    @staticmethod
    def _db(project_id):
        db = AsyncMock()
        db.claim_due_knowledge_materializations.return_value = []
        db.list_datasources.return_value = []
        db.fetch.return_value = [{"project_id": project_id}]
        db.mark_knowledge_projections_synced.return_value = 0
        return db

    @pytest.mark.asyncio
    async def test_purge_runs_after_a_current_reindex_when_enabled(self):
        project_id = uuid.uuid4()
        order = []

        async def reindex(**kwargs):
            order.append("reindex")
            return {"status": "up-to-date"}

        async def purge(**kwargs):
            order.append(("purge", kwargs["kb_id"]))
            return {"purged": 1}

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
                purge_fn=purge,
                purge_enabled_fn=lambda: True,
            )
        assert order == ["reindex", ("purge", project_id)]
        assert n == 1  # a purge that removed something counts as work

    @pytest.mark.asyncio
    async def test_purge_is_skipped_when_disabled(self):
        project_id = uuid.uuid4()
        purge = AsyncMock(return_value={"purged": 0})
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
                purge_fn=purge,
                purge_enabled_fn=lambda: False,
            )
        purge.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_purge_is_skipped_when_the_index_is_not_current(self):
        project_id = uuid.uuid4()
        purge = AsyncMock(return_value={"purged": 0})
        with patch(
            "services.kb_reindex.resolve_kb_repo",
            AsyncMock(return_value=MagicMock(repo="r", branch="main", forge="gitea")),
        ):
            await kb_sweep_tick(
                postgres_db=self._db(project_id),
                store=MagicMock(),
                gitea_client=MagicMock(),
                embedding_service=MagicMock(),
                reindex_fn=AsyncMock(return_value={"status": "already-indexing"}),
                purge_fn=purge,
                purge_enabled_fn=lambda: True,
            )
        purge.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_purge_failure_does_not_fail_the_sweep(self):
        project_id = uuid.uuid4()
        with patch(
            "services.kb_reindex.resolve_kb_repo",
            AsyncMock(return_value=MagicMock(repo="r", branch="main", forge="gitea")),
        ):
            n = await kb_sweep_tick(
                postgres_db=self._db(project_id),
                store=MagicMock(),
                gitea_client=MagicMock(),
                embedding_service=MagicMock(),
                reindex_fn=AsyncMock(return_value={"status": "completed"}),
                purge_fn=AsyncMock(side_effect=RuntimeError("purge broke")),
                purge_enabled_fn=lambda: True,
            )
        assert n == 1  # the completed reindex still counts
