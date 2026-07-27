"""Project backlog / idea pipeline — spec:
docs/superpowers/specs/2026-07-26-project-backlog-pipeline-design.md
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# =============================================================================
# Helpers (Task 3: priority on the agent tool surface)
# =============================================================================


def _kb_context(job_id=None):
    """A ToolContext with Neo4j disabled (kg=None) and a mocked pgvector store.

    Mirrors the proven kgless fixture in
    tests/test_knowledge_tools.py::_make_context_no_kg. The task-3 brief's own
    ``_kb_context()`` called ``create_knowledge_tools`` imported from
    ``knowledge_tools.py`` — that name only lives in
    ``src/tools/knowledge/__init__.py`` (a thin wrapper over
    ``create_kb_tools``), so the brief's import raises ImportError. No
    existing test constructs these tools that way; ``create_kb_tools`` (used
    below) is the proven path, already exercised with a bare ``MagicMock`` in
    tests/test_tool_vocabularies.py::test_kb_tool_params_are_constrained.
    """
    ctx = MagicMock()
    ctx.project_id = str(uuid.uuid4())
    ctx.project_ids = [ctx.project_id]
    ctx.job_id = job_id or "aaaaaaaa-0000-0000-0000-000000000001"
    ctx.config = {"current_phase": 2}
    ctx.knowledge_graph = None
    ctx.knowledge_store = AsyncMock()
    return ctx


def _kb_context_with_kg(job_id=None):
    """A ToolContext with Neo4j present (kg is a MagicMock), store present."""
    ctx = _kb_context(job_id=job_id)
    ctx.knowledge_graph = MagicMock()
    return ctx


def _make_tools(ctx):
    """Create kb tools with a mocked context, by name.

    Patches ``asyncio`` only during construction (forcing the
    ``asyncio.run()`` fallback at invocation time) — same pattern as
    tests/test_knowledge_tools.py::_make_tools.
    """
    from src.tools.knowledge.knowledge_tools import create_kb_tools

    with patch("src.tools.knowledge.knowledge_tools.asyncio") as mock_asyncio:
        mock_asyncio.get_running_loop.side_effect = RuntimeError("no loop")
        tools = create_kb_tools(ctx)
    return {t.name: t for t in tools}


def _capture_workspace():
    """A mock workspace_manager that records write_file(path, content) calls."""
    ws = MagicMock()
    writes: dict = {}
    ws.write_file.side_effect = lambda rel, content: writes.__setitem__(rel, content)
    return ws, writes


class TestPriorityRoundTrip:
    def test_render_emits_priority_word(self):
        from src.tools.knowledge.knowledge_tools import _render_note_md

        md = _render_note_md(
            {"id": "feature-x", "type": "feature", "content": "body", "priority": 0}
        )
        assert "priority: high" in md

    def test_render_omits_priority_when_absent(self):
        """Existing notes must not gain noise in their frontmatter."""
        from src.tools.knowledge.knowledge_tools import _render_note_md

        md = _render_note_md({"id": "d-1", "type": "decision", "content": "body"})
        assert "priority:" not in md

    def test_reindex_parses_priority_word_to_rank(self):
        # note_fields(path, fm, body) is the real signature (kb_reindex.py:157)
        # — it takes already-parsed frontmatter as a dict, not raw markdown.
        from orchestrator.services.kb_reindex import note_fields

        parsed = note_fields(
            "feature-x.md",
            {"id": "feature-x", "type": "feature", "priority": "high"},
            "# T\nbody",
        )
        assert parsed["priority"] == 0

    def test_reindex_defaults_unknown_priority_to_normal(self):
        """Frontmatter is human-editable; a typo must not fail the row."""
        from orchestrator.services.kb_reindex import note_fields

        parsed = note_fields(
            "feature-x.md",
            {"id": "feature-x", "type": "feature", "priority": "URGENT!!"},
            "# T\nb",
        )
        assert parsed["priority"] == 1


# =============================================================================
# Task 3: priority on kb_write / kb_update / kb_list
# =============================================================================


class TestKbToolPrioritySchema:
    """Step 1: agents must be able to see and set priority on both tools.

    Schema-based (``args_schema.model_fields``) rather than closure-based
    (``inspect.signature(tool.func)``): the task instructions call this out
    as the more robust option, since it doesn't depend on ``.func`` being
    reachable. ``.func`` *is* reachable in this LangChain version (see
    tests/test_knowledge_tools.py::_invoke_unvalidated), so either would have
    worked; the schema form was chosen for consistency with
    test_tool_vocabularies.py, which already asserts against ``args_schema``
    for every other closed-vocabulary kb_* parameter.
    """

    def test_kb_write_accepts_priority(self):
        """Agents must be able to file a ticket at a priority in one call."""
        tools = _make_tools(_kb_context())
        assert "priority" in tools["kb_write"].args_schema.model_fields

    def test_kb_update_accepts_priority(self):
        tools = _make_tools(_kb_context())
        assert "priority" in tools["kb_update"].args_schema.model_fields

    def test_kb_write_priority_defaults_to_normal(self):
        tools = _make_tools(_kb_context())
        field = tools["kb_write"].args_schema.model_fields["priority"]
        assert field.default == "normal"

    def test_kb_update_priority_defaults_to_none(self):
        """None must mean "leave unchanged" — kb_update cannot default to a
        priority word, or every status-only edit would reset the rank."""
        tools = _make_tools(_kb_context())
        field = tools["kb_update"].args_schema.model_fields["priority"]
        assert field.default is None


class TestKbWritePriorityPersistence:
    """Proves priority reaches the actual persistence call (ks.upsert_note)
    as the correct rank integer — a schema assertion alone doesn't show this
    (task-3 test-quality bar: "a signature/schema assertion alone does not
    prove the value reaches storage")."""

    def test_high_priority_arrives_as_rank_zero(self):
        ctx = _kb_context()
        ctx.knowledge_store.get_note_by_slug = AsyncMock(return_value=None)
        tools = _make_tools(ctx)
        tools["kb_write"].invoke(
            {
                "title": "Add dark mode",
                "type": "feature",
                "content": "body",
                "priority": "high",
            }
        )
        kwargs = ctx.knowledge_store.upsert_note.call_args.kwargs
        assert kwargs["priority"] == 0

    def test_low_priority_arrives_as_rank_two(self):
        ctx = _kb_context()
        ctx.knowledge_store.get_note_by_slug = AsyncMock(return_value=None)
        tools = _make_tools(ctx)
        tools["kb_write"].invoke(
            {
                "title": "Someday idea",
                "type": "idea",
                "content": "body",
                "priority": "low",
            }
        )
        kwargs = ctx.knowledge_store.upsert_note.call_args.kwargs
        assert kwargs["priority"] == 2

    def test_omitted_priority_defaults_to_rank_one(self):
        ctx = _kb_context()
        ctx.knowledge_store.get_note_by_slug = AsyncMock(return_value=None)
        tools = _make_tools(ctx)
        tools["kb_write"].invoke(
            {"title": "Some issue", "type": "issue", "content": "body"}
        )
        kwargs = ctx.knowledge_store.upsert_note.call_args.kwargs
        assert kwargs["priority"] == 1


class TestKbWritePriorityNonTicketUnaffected:
    """Global constraint: an existing decision/learning note keeps identical
    frontmatter — priority is scoped to feature/issue/idea tickets."""

    def test_decision_note_gets_no_priority_frontmatter_line(self):
        ws, writes = _capture_workspace()
        ctx = _kb_context()
        ctx.workspace_manager = ws
        ctx.has_git.return_value = True
        ctx.knowledge_store.get_note_by_slug = AsyncMock(return_value=None)
        tools = _make_tools(ctx)
        tools["kb_write"].invoke(
            {"title": "Chose JWT", "type": "decision", "content": "body"}
        )
        md = writes["knowledge/chose-jwt.md"]
        assert "priority:" not in md

    def test_feature_note_gets_priority_frontmatter_line(self):
        ws, writes = _capture_workspace()
        ctx = _kb_context()
        ctx.workspace_manager = ws
        ctx.has_git.return_value = True
        ctx.knowledge_store.get_note_by_slug = AsyncMock(return_value=None)
        tools = _make_tools(ctx)
        tools["kb_write"].invoke(
            {
                "title": "Add dark mode",
                "type": "feature",
                "content": "body",
                "priority": "high",
            }
        )
        md = writes["knowledge/add-dark-mode.md"]
        assert "priority: high" in md


def _existing_ticket(**over):
    """A get_note_by_slug-shaped dict for a feature/issue/idea ticket."""
    base = {
        "id": "n1",
        "title": "T",
        "type": "feature",
        "status": "active",
        "content": "body",
        "confidence": None,
        "tags": [],
        "keywords": [],
        "job_id": None,
        "phase": None,
        "created": None,
        "modified": None,
        "priority": 0,  # existing: high
    }
    base.update(over)
    return base


class TestKbUpdatePriorityPreservation:
    """kb_update(priority=None) must never silently reset an existing
    ticket's priority to normal (Global constraint). This was a dormant hole:
    fe8dd707 (task 2) fixed the storage layer's binding but nothing called it
    with a real value yet — this wires the caller up.
    """

    def test_omitted_priority_preserves_existing_high(self):
        ctx = _kb_context()
        ctx.knowledge_store.get_note_by_slug = AsyncMock(
            return_value=_existing_ticket(priority=0)
        )
        tools = _make_tools(ctx)
        result = tools["kb_update"].invoke({"note": "n1", "status": "resolved"})
        kwargs = ctx.knowledge_store.upsert_note.call_args.kwargs
        assert kwargs["priority"] == 0
        # The status change itself still goes through — this isn't a no-op.
        assert "status → resolved" in result

    def test_explicit_priority_overrides_existing(self):
        ctx = _kb_context()
        ctx.knowledge_store.get_note_by_slug = AsyncMock(
            return_value=_existing_ticket(priority=0)
        )
        tools = _make_tools(ctx)
        tools["kb_update"].invoke({"note": "n1", "priority": "low"})
        kwargs = ctx.knowledge_store.upsert_note.call_args.kwargs
        assert kwargs["priority"] == 2


class TestKbUpdatePriorityPreservationWithNeo4j:
    """The kg-enabled path must also preserve priority on omission. Neo4j
    itself has no priority property (Tasks 1-2 only added it to the pgvector
    row), so pgvector via ks.get_note_by_slug is the only place the prior
    value can come from — without this lookback, any kb_update that omits
    priority on a Neo4j-backed ticket would reset it to normal.
    """

    def test_omitted_priority_preserves_existing_high_via_pgvector_lookback(self):
        ctx = _kb_context_with_kg()
        ctx.knowledge_graph.update_note.return_value = True
        ctx.knowledge_graph.read_note.return_value = {
            "type": "feature",
            "title": "Add dark mode",
            "content": "body",
            "status": "active",
        }
        ctx.knowledge_store.get_note_by_slug = AsyncMock(
            return_value={"priority": 0}  # pgvector's last-known value: high
        )
        tools = _make_tools(ctx)
        tools["kb_update"].invoke({"note": "add-dark-mode", "status": "resolved"})
        kwargs = ctx.knowledge_store.upsert_note.call_args.kwargs
        assert kwargs["priority"] == 0

    def test_lookback_failure_leaves_priority_and_frontmatter_untouched(self):
        """Fix round 1, Finding 1 repro: kg-enabled, get_note_by_slug raises,
        existing ticket is priority=high, kb_update omits priority.

        Before the fix this seeded DEFAULT_PRIORITY_RANK (normal) ahead of
        the lookback and the except swallowed the raise, so ks.upsert_note
        was called with priority=1 and the rewritten OKF file claimed
        "priority: normal" -- silently clobbering "high". A failed lookback
        must instead leave priority as None (the "unknown, don't touch"
        sentinel COALESCE'd against the real row in knowledge_store.py), so
        neither the DB row nor the frontmatter ever assert a guessed value.
        """
        ws, writes = _capture_workspace()
        ctx = _kb_context_with_kg()
        ctx.workspace_manager = ws
        ctx.has_git.return_value = True
        ctx.knowledge_graph.update_note.return_value = True
        ctx.knowledge_graph.read_note.return_value = {
            "type": "feature",
            "title": "Add dark mode",
            "content": "body",
            "status": "active",
        }
        ctx.knowledge_store.get_note_by_slug = AsyncMock(
            side_effect=Exception("pgvector hiccup")
        )
        tools = _make_tools(ctx)
        result = tools["kb_update"].invoke(
            {"note": "add-dark-mode", "status": "resolved"}
        )

        # (c) a pgvector hiccup must not block the rest of the update.
        assert "status → resolved" in result

        # (a) nothing writes a priority that was neither supplied by the
        # caller nor read from storage -- None reaches the persistence call,
        # which is the sentinel knowledge_store.py's upsert_note COALESCEs
        # against the row's real (unknown-to-us) current value instead of
        # overwriting it. See TestUpsertNotePriorityCoalesceSentinel in
        # test_knowledge_store.py for proof the SQL side honors it.
        kwargs = ctx.knowledge_store.upsert_note.call_args.kwargs
        assert kwargs["priority"] is None

        # The frontmatter must not assert a value we don't actually know --
        # _render_note_md omits the line entirely when the key is absent.
        md = writes["knowledge/add-dark-mode.md"]
        assert "priority:" not in md

    def test_lookback_failure_is_logged_not_swallowed(self, caplog):
        """Finding 1(b): the failure must be visible, not silently eaten."""
        import logging

        ctx = _kb_context_with_kg()
        ctx.knowledge_graph.update_note.return_value = True
        ctx.knowledge_graph.read_note.return_value = {
            "type": "feature",
            "title": "Add dark mode",
            "content": "body",
            "status": "active",
        }
        ctx.knowledge_store.get_note_by_slug = AsyncMock(
            side_effect=Exception("pgvector hiccup")
        )
        tools = _make_tools(ctx)
        with caplog.at_level(
            logging.WARNING, logger="src.tools.knowledge.knowledge_tools"
        ):
            tools["kb_update"].invoke({"note": "add-dark-mode", "status": "resolved"})
        assert any(
            "priority" in record.message and "pgvector hiccup" in record.message
            for record in caplog.records
        )


class TestKbListPriorityDisplay:
    """Step 5 (corrected): priority is visible for backlog tickets, and the
    line is byte-identical to the pre-task-3 format for every other type."""

    def test_ticket_row_shows_priority_word(self):
        ctx = _kb_context()
        ctx.knowledge_store.list_notes = AsyncMock(
            return_value=[
                {
                    "id": "add-dark-mode",
                    "title": "Add dark mode",
                    "type": "feature",
                    "status": "active",
                    "confidence": None,
                    "priority": 0,
                }
            ]
        )
        tools = _make_tools(ctx)
        result = tools["kb_list"].invoke({})
        lines = [line for line in result.splitlines() if "add-dark-mode" in line]
        assert len(lines) == 1
        assert lines[0] == (
            "● **add-dark-mode** — Add dark mode (feature [priority: high])"
        )

    def test_non_ticket_row_is_byte_identical_to_pre_task3_format(self):
        ctx = _kb_context()
        ctx.knowledge_store.list_notes = AsyncMock(
            return_value=[
                {
                    "id": "chose-jwt",
                    "title": "Chose JWT",
                    "type": "decision",
                    "status": "active",
                    "confidence": "high",
                }
            ]
        )
        tools = _make_tools(ctx)
        result = tools["kb_list"].invoke({})
        lines = [line for line in result.splitlines() if "chose-jwt" in line]
        assert len(lines) == 1
        assert lines[0] == "● **chose-jwt** — Chose JWT (decision [high])"
        assert "priority" not in lines[0]


# =============================================================================
# Task 4: the pool query and its renderer
# =============================================================================


def _row(note_id, note_type="feature", title="T", priority=1):
    return {
        "note_id": note_id,
        "note_type": note_type,
        "title": title,
        "priority": priority,
    }


class TestRenderBacklogBlock:
    def test_counts_line_comes_first_and_breaks_down_by_priority(self):
        """The cap hides the tail; the counts line is what tells the reader a
        tail exists at all. It must lead the block."""
        from orchestrator.services.project_backlog import render_backlog_block

        rows = [_row("a", priority=0), _row("b", priority=1)]
        counts = {0: 12, 1: 15, 2: 7}
        block = render_backlog_block(rows, counts, limit=20)

        first = block.splitlines()[0]
        assert first == (
            "PROJECT BACKLOG — 34 open: 12 high, 15 normal, 7 low (showing top 20)"
        )

    def test_in_progress_line_precedes_the_list(self):
        from orchestrator.services.project_backlog import render_backlog_block

        block = render_backlog_block(
            [_row("a")],
            {1: 1},
            in_progress={"note_id": "issue-deploy", "title": "Deploy docs",
                         "priority": 0, "note_type": "issue"},
        )
        lines = block.splitlines()
        assert lines[1].startswith("IN PROGRESS: [high] issue-deploy — Deploy docs")
        assert lines[2].lstrip().startswith("[normal]")

    def test_no_in_progress_renders_none(self):
        from orchestrator.services.project_backlog import render_backlog_block

        block = render_backlog_block([_row("a")], {1: 1})
        assert "IN PROGRESS: (none)" in block

    def test_remainder_is_reported_when_capped(self):
        from orchestrator.services.project_backlog import render_backlog_block

        rows = [_row(f"n{i}") for i in range(3)]
        block = render_backlog_block(rows, {1: 10}, limit=3)
        assert "… 7 more" in block

    def test_empty_pool_tells_the_agent_to_file_tickets(self):
        """An empty pool must not render an empty void — the loop's own agents
        are the ones expected to fill it."""
        from orchestrator.services.project_backlog import render_backlog_block

        block = render_backlog_block([], {})
        assert "0 open" in block
        assert "kb_write" in block


class _FakeAcquire:
    """Async-context-manager pool.acquire() double.

    Mirrors the idiom in tests/test_loop_campaign_scheduling.py::_FakeAcquire
    and tests/test_delegation_timeout_outage.py::_FakeAcquire rather than
    inventing a bespoke one here.
    """

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *a):
        return False


def _fake_vector_db(conn):
    """A pool double whose ``.acquire()`` yields ``conn`` via _FakeAcquire."""
    vector_db = MagicMock()
    vector_db.acquire = MagicMock(return_value=_FakeAcquire(conn))
    return vector_db


class TestFetchBacklog:
    @pytest.mark.asyncio
    async def test_excludes_the_in_progress_initiative(self):
        """The overseer must never be offered a ticket already underway."""
        from orchestrator.services.project_backlog import fetch_backlog

        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=[])
        vector_db = _fake_vector_db(conn)

        await fetch_backlog(vector_db, "p-1", exclude_note_id="issue-x")

        first_sql = conn.fetch.await_args_list[0].args[0]
        assert "note_id <> $2" in first_sql
        assert conn.fetch.await_args_list[0].args[2] == "issue-x"

    @pytest.mark.asyncio
    async def test_counts_query_also_excludes_the_in_progress_initiative(self):
        """The pool listing and its counts line must agree on what "open"
        means -- a mismatch here would make the header's total lie about
        what the list below it actually shows."""
        from orchestrator.services.project_backlog import fetch_backlog

        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=[])
        vector_db = _fake_vector_db(conn)

        await fetch_backlog(vector_db, "p-1", exclude_note_id="issue-x")

        second_sql = conn.fetch.await_args_list[1].args[0]
        assert "note_id <> $2" in second_sql
        assert conn.fetch.await_args_list[1].args[2] == "issue-x"

    @pytest.mark.asyncio
    async def test_no_exclusion_binds_none(self):
        """The default (no in-progress campaign) must bind SQL NULL, not the
        string 'None' -- the WHERE clause's `$2::text IS NULL` branch depends
        on this. (Position $2, not $3: fix round 1, Finding 1 inlines the
        note-type filter as a literal, so it no longer occupies a $-slot.)"""
        from orchestrator.services.project_backlog import fetch_backlog

        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=[])
        vector_db = _fake_vector_db(conn)

        await fetch_backlog(vector_db, "p-1")

        assert conn.fetch.await_args_list[0].args[2] is None

    @pytest.mark.asyncio
    async def test_orders_by_priority_then_age_ascending(self):
        """Pin the actual ORDER BY clause: a rewrite to DESC (or to created_at
        first) would still exercise the same mocks and pass a looser test --
        this one fails if the ordering regresses."""
        from orchestrator.services.project_backlog import fetch_backlog

        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=[])
        vector_db = _fake_vector_db(conn)

        await fetch_backlog(vector_db, "p-1")

        first_sql = " ".join(conn.fetch.await_args_list[0].args[0].split())
        assert "ORDER BY priority ASC, created_at ASC" in first_sql

    @pytest.mark.asyncio
    async def test_note_types_are_inlined_as_a_sql_literal_not_a_bound_param(self):
        """Fix round 1, Finding 1: measured on pgvector/pgvector:pg15 (303k
        rows / 300 projects), a bound `note_type = ANY($n::text[])` cannot be
        proven under `force_generic_plan` to imply idx_knowledge_backlog's
        literal partial predicate -- the planner fell back to a full
        project scan + Filter + Sort for both queries, and the index went
        unused. The fix inlines the note-type filter as a SQL literal,
        derived from BACKLOG_NOTE_TYPES once so the tuple and the SQL can
        never drift apart. A future "tidy-up" back to `= ANY($n)` must fail
        this test."""
        from orchestrator.services.project_backlog import (
            BACKLOG_NOTE_TYPES,
            fetch_backlog,
        )

        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=[])
        vector_db = _fake_vector_db(conn)

        await fetch_backlog(vector_db, "p-1")

        expected_literal = (
            "note_type IN (" + ", ".join(f"'{t}'" for t in BACKLOG_NOTE_TYPES) + ")"
        )
        for call in conn.fetch.await_args_list:
            sql = " ".join(call.args[0].split())
            assert "note_type IN (" in sql
            assert expected_literal in sql
            assert "= ANY(" not in sql

    @pytest.mark.asyncio
    async def test_limit_is_bound_as_the_third_row_query_parameter(self):
        """Position $3, not $4: fix round 1, Finding 1 inlines the note-type
        filter as a literal, so it no longer occupies a $-slot."""
        from orchestrator.services.project_backlog import fetch_backlog

        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=[])
        vector_db = _fake_vector_db(conn)

        await fetch_backlog(vector_db, "p-1", limit=5)

        assert conn.fetch.await_args_list[0].args[3] == 5

    @pytest.mark.asyncio
    async def test_high_priority_rank_zero_survives_the_round_trip(self):
        """Rank 0 (high) is falsy in Python. A `row["priority"] or None` or
        `{k: v for k, v in ... if k}` style bug would silently drop it --
        prove a rank-0 row and its count both come back intact."""
        from orchestrator.services.project_backlog import fetch_backlog

        conn = MagicMock()
        conn.fetch = AsyncMock(
            side_effect=[
                [
                    {
                        "note_id": "issue-x",
                        "note_type": "issue",
                        "title": "T",
                        "priority": 0,
                    }
                ],
                [{"priority": 0, "n": 3}],
            ]
        )
        vector_db = _fake_vector_db(conn)

        rows, counts = await fetch_backlog(vector_db, "p-1")

        assert rows == [
            {
                "note_id": "issue-x",
                "note_type": "issue",
                "title": "T",
                "priority": 0,
            }
        ]
        assert 0 in counts
        assert counts[0] == 3
