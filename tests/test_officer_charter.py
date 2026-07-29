"""S7 — charter + officer KB note types (docs/features/centurion.md §5).

Covers:
  1. Note-type vocabulary lockstep: NOTE_TYPES / reindexer / vector migration
     0015 stay in agreement (the reindexer silently rewrites unknown types to
     'learning' — drift here is silent data corruption).
  2. Charter write authority: workers can never create or edit 'charter'
     notes (trust boundary — recon workers ingest untrusted content);
     one ACTIVE charter per project.
  3. Sole-store honesty: on lite sessions (no Neo4j, no git) a failed
     pgvector write fails the tool instead of claiming "Created".
  4. Charter fetch (KnowledgeStore.get_charter_note) — project_id-keyed,
     path-agnostic, active-only.
  5. Charter injection: dedicated pair, first in the injection zone,
     excluded from summarization; gate strict on officer.enabled/conference.
  6. Config plumbing: officer.conference parses in the loader and is admitted
     by the session override sanitizer.
"""

import re
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.core.knowledge_injection import (
    CHARTER_TOOL_CALL_ID_PREFIX,
    create_charter_injection_messages,
)
from src.core.loader import OfficerConfig, _parse_officer_config
from src.core.workspace_injection import is_workspace_injection_message
from src.persistent_graph import (
    _charter_injection_enabled,
    _inject_context_pairs,
)
from src.services.knowledge_graph import NOTE_TYPES
from src.services.knowledge_store import KB_TTL_BY_NOTE_TYPE, KnowledgeStore
from src.tools.knowledge.knowledge_tools import create_kb_tools

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "orchestrator"
    / "database"
    / "migrations"
    / "vector"
    / "0015_kb_officer_note_types.sql"
)


# =============================================================================
# 1. Vocabulary lockstep
# =============================================================================


class TestNoteTypeLockstep:
    def test_officer_types_in_agent_vocabulary(self):
        assert "charter" in NOTE_TYPES
        assert "report" in NOTE_TYPES

    def test_reindexer_vocabulary_matches_agent(self):
        from orchestrator.services.kb_reindex import VALID_NOTE_TYPES

        assert VALID_NOTE_TYPES == set(NOTE_TYPES)

    def test_migration_covers_full_vocabulary(self):
        sql = _MIGRATION.read_text()
        quoted = set(re.findall(r"'([a-z_]+)'", sql))
        for note_type in NOTE_TYPES:
            assert note_type in quoted, f"migration 0015 missing '{note_type}'"

    def test_officer_types_are_durable(self):
        # Explicit None (no TTL), not merely absent — the charter is infinite
        # by definition; reports are retired by gardening, not a clock.
        assert KB_TTL_BY_NOTE_TYPE["charter"] is None
        assert KB_TTL_BY_NOTE_TYPE["report"] is None


# =============================================================================
# kb-tool harness (kg-less = the session shape; real asyncio so AsyncMock
# returns flow through _run_async's asyncio.run fallback)
# =============================================================================


def _session_context(thread_id="11111111-2222-3333-4444-555555555555"):
    ctx = MagicMock()
    ctx.project_id = str(uuid.uuid4())
    ctx.project_ids = [ctx.project_id]
    ctx.job_id = ctx.project_id
    ctx.config = {"current_phase": None}
    ctx.knowledge_graph = None
    ctx.knowledge_store = AsyncMock()
    ctx.knowledge_bindings = []
    ctx._thread_id = thread_id
    ctx.has_git = MagicMock(return_value=False)
    return ctx


def _worker_context():
    ctx = _session_context(thread_id=None)
    ctx.job_id = str(uuid.uuid4())
    return ctx


def _tool(ctx, name):
    for t in create_kb_tools(ctx):
        if t.name == name:
            return t
    raise KeyError(name)


# =============================================================================
# 2. Charter write authority
# =============================================================================


class TestCharterWriteAuthority:
    def test_worker_cannot_create_charter(self):
        ctx = _worker_context()
        result = _tool(ctx, "kb_write").func(
            title="Charter", type="charter", content="orders"
        )
        assert "cannot be written from a worker job" in result
        ctx.knowledge_store.upsert_note.assert_not_called()

    def test_worker_can_file_reports(self):
        ctx = _worker_context()
        ctx.knowledge_store.get_note_by_slug.return_value = None
        ctx.knowledge_store.upsert_note.return_value = uuid.uuid4()
        result = _tool(ctx, "kb_write").func(
            title="Recon findings", type="report", content="findings"
        )
        assert "Error" not in result
        ctx.knowledge_store.upsert_note.assert_called_once()
        assert ctx.knowledge_store.upsert_note.call_args.kwargs["note_type"] == "report"

    def test_session_creates_charter_when_none_exists(self):
        ctx = _session_context()
        ctx.knowledge_store.get_charter_note.return_value = None
        ctx.knowledge_store.get_note_by_slug.return_value = None
        ctx.knowledge_store.upsert_note.return_value = uuid.uuid4()
        result = _tool(ctx, "kb_write").func(
            title="Century charter", type="charter", content="orders"
        )
        assert "Error" not in result
        ctx.knowledge_store.upsert_note.assert_called_once()

    def test_second_active_charter_refused(self):
        ctx = _session_context()
        ctx.knowledge_store.get_charter_note.return_value = {
            "id": "century-charter",
            "type": "charter",
            "status": "active",
        }
        result = _tool(ctx, "kb_write").func(
            title="Another charter", type="charter", content="orders"
        )
        assert "already has an active charter" in result
        assert "century-charter" in result
        ctx.knowledge_store.upsert_note.assert_not_called()

    def test_charter_lookup_failure_refuses_write(self):
        # Fail-closed: if we cannot verify uniqueness we do not write.
        ctx = _session_context()
        ctx.knowledge_store.get_charter_note.side_effect = RuntimeError("db down")
        result = _tool(ctx, "kb_write").func(
            title="Charter", type="charter", content="orders"
        )
        assert "could not verify" in result
        ctx.knowledge_store.upsert_note.assert_not_called()

    def test_worker_cannot_update_charter_kgless(self):
        ctx = _worker_context()
        ctx.knowledge_store.get_note_by_slug.return_value = {
            "id": "century-charter",
            "type": "charter",
            "status": "active",
            "content": "orders",
            "tags": [],
        }
        result = _tool(ctx, "kb_update").func(
            note="century-charter", content="my orders now"
        )
        assert "cannot be written from a worker job" in result
        ctx.knowledge_store.upsert_note.assert_not_called()

    def test_session_can_update_charter_kgless(self):
        ctx = _session_context()
        ctx.knowledge_store.get_note_by_slug.return_value = {
            "id": "century-charter",
            "type": "charter",
            "status": "active",
            "content": "orders",
            "tags": [],
            "keywords": [],
            "priority": 1,
        }
        ctx.knowledge_store.upsert_note.return_value = uuid.uuid4()
        result = _tool(ctx, "kb_update").func(
            note="century-charter", content="posture: demo Friday"
        )
        assert "cannot be written" not in result
        ctx.knowledge_store.upsert_note.assert_called_once()

    def test_worker_cannot_update_charter_graph_path(self):
        ctx = _worker_context()
        ctx.knowledge_graph = MagicMock()
        ctx.knowledge_graph.read_note.return_value = {
            "id": "century-charter",
            "type": "charter",
        }
        result = _tool(ctx, "kb_update").func(
            note="century-charter", content="my orders now"
        )
        assert "cannot be written from a worker job" in result
        ctx.knowledge_graph.update_note.assert_not_called()

    def test_worker_updates_ordinary_notes_freely(self):
        ctx = _worker_context()
        ctx.knowledge_store.get_note_by_slug.return_value = {
            "id": "some-learning",
            "type": "learning",
            "status": "active",
            "content": "old",
            "tags": [],
            "keywords": [],
            "priority": 1,
        }
        ctx.knowledge_store.upsert_note.return_value = uuid.uuid4()
        result = _tool(ctx, "kb_update").func(note="some-learning", content="new")
        assert "cannot be written" not in result
        ctx.knowledge_store.upsert_note.assert_called_once()


# =============================================================================
# 3. Sole-store honesty (risk 11)
# =============================================================================


class TestSoleStoreWriteHonesty:
    def test_pgvector_failure_fails_tool_on_lite_session(self):
        ctx = _session_context()  # kg None + has_git False = sole store
        ctx.knowledge_store.get_note_by_slug.return_value = None
        ctx.knowledge_store.upsert_note.side_effect = RuntimeError("pool gone")
        result = _tool(ctx, "kb_write").func(
            title="State note", type="state", content="the century stands"
        )
        assert "NOT saved" in result

    def test_pgvector_failure_stays_nonfatal_with_git(self):
        ctx = _session_context()
        ctx.has_git = MagicMock(return_value=True)
        # Dual-write path needs a workspace write to succeed.
        ctx.workspace_manager.write_file = MagicMock()
        ctx.knowledge_store.get_note_by_slug.return_value = None
        ctx.knowledge_store.upsert_note.side_effect = RuntimeError("pool gone")
        result = _tool(ctx, "kb_write").func(
            title="State note", type="state", content="the century stands"
        )
        assert "NOT saved" not in result


# =============================================================================
# 4. get_charter_note
# =============================================================================


class TestGetCharterNote:
    @pytest.mark.asyncio
    async def test_query_shape_and_row_mapping(self):
        db = MagicMock()
        captured: dict = {}

        async def fetchrow(sql, *params):
            captured["sql"] = sql
            captured["params"] = params
            return {
                "note_id": "century-charter",
                "title": "Century charter",
                "note_type": "charter",
                "status": "active",
                "content": "orders",
                "job_id": None,
                "created_at": None,
                "modified_at": None,
            }

        db.fetchrow = fetchrow
        store = KnowledgeStore(db, embedding_service=MagicMock())
        pid = uuid.uuid4()
        note = await store.get_charter_note(pid)

        assert note["id"] == "century-charter"
        assert note["type"] == "charter"
        sql = captured["sql"]
        assert "note_type = 'charter'" in sql
        assert "status = 'active'" in sql
        assert "project_id" in sql
        # Lite sessions write pathless rows — the charter fetch must not
        # filter on path like get_note_by_slug does.
        assert "path" not in sql
        assert captured["params"] == (pid,)

    @pytest.mark.asyncio
    async def test_no_charter_returns_none(self):
        db = MagicMock()

        async def fetchrow(sql, *params):
            return None

        db.fetchrow = fetchrow
        store = KnowledgeStore(db, embedding_service=MagicMock())
        assert await store.get_charter_note(uuid.uuid4()) is None


# =============================================================================
# 5. Charter injection
# =============================================================================


class TestCharterInjection:
    def test_pair_shape_and_summarization_exclusion(self):
        ai, tool = create_charter_injection_messages("standing orders")
        assert isinstance(ai, AIMessage) and isinstance(tool, ToolMessage)
        assert ai.tool_calls[0]["id"].startswith(CHARTER_TOOL_CALL_ID_PREFIX)
        assert tool.tool_call_id == ai.tool_calls[0]["id"]
        assert tool.content == "standing orders"
        # Excluded from summarization — re-injected fresh each turn, so
        # compaction can never strip the standing orders.
        assert is_workspace_injection_message(ai)
        assert is_workspace_injection_message(tool)

    def test_charter_lands_first_in_injection_zone(self):
        prepared = [HumanMessage(content="wake")]
        n = _inject_context_pairs(
            prepared,
            manager_injection=[],
            memory_block="memories",
            knowledge_block="notes",
            citation_feedback_block="",
            charter_block="orders",
        )
        assert n == 6
        # Zone starts after the trailing human message.
        first_ai = prepared[1]
        assert isinstance(first_ai, AIMessage)
        assert first_ai.tool_calls[0]["id"].startswith(CHARTER_TOOL_CALL_ID_PREFIX)
        assert prepared[2].content == "orders"

    def test_no_charter_block_no_pair(self):
        prepared = [HumanMessage(content="wake")]
        n = _inject_context_pairs(
            prepared,
            manager_injection=[],
            memory_block="",
            knowledge_block="",
            citation_feedback_block="",
        )
        assert n == 0

    def test_gate_officer_enabled(self):
        cfg = MagicMock()
        cfg.officer = OfficerConfig(enabled=True)
        assert _charter_injection_enabled(cfg) is True

    def test_gate_conference(self):
        cfg = MagicMock()
        cfg.officer = OfficerConfig(enabled=False, conference=True)
        assert _charter_injection_enabled(cfg) is True

    def test_gate_off_by_default(self):
        cfg = MagicMock()
        cfg.officer = OfficerConfig()
        assert _charter_injection_enabled(cfg) is False

    def test_gate_strict_against_mock_configs(self):
        # MagicMock attrs are truthy; the gate demands `is True` (S1 lesson).
        cfg = MagicMock()
        assert _charter_injection_enabled(cfg) is False

    def test_gate_no_officer(self):
        cfg = MagicMock()
        cfg.officer = None
        assert _charter_injection_enabled(cfg) is False


# =============================================================================
# 6. Config plumbing
# =============================================================================


class TestConferenceConfig:
    def test_loader_parses_conference(self):
        cfg = _parse_officer_config({"officer": {"conference": True}})
        assert cfg.conference is True
        assert cfg.enabled is False

    def test_loader_default_off(self):
        cfg = _parse_officer_config({"officer": {"enabled": True}})
        assert cfg.conference is False

    def test_sanitizer_admits_conference(self):
        import main

        cleaned = main._validated_session_officer_override(
            {"officer": {"conference": True}}
        )
        assert cleaned == {"conference": True}

    def test_sanitizer_still_rejects_unknown_keys(self):
        from fastapi import HTTPException

        import main

        with pytest.raises(HTTPException):
            main._validated_session_officer_override({"officer": {"conferance": True}})
