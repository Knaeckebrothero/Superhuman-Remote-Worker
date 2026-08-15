"""Backlog ticket plumbing — B2 of docs/features/officer_backlog_pools.md.

B1 gave tags a meaning; B2 makes them writable, queryable, and — the part that
matters — forgeable only by the officer. Three properties are load-bearing and
each has a failure mode worth naming:

* **Tags can be removed.** ``ready`` used to be a one-way door: once stamped, a
  ticket could never be un-armed, and a category change left the ticket
  matching two pools at once.
* **A worker cannot arm a ticket.** ``ready`` is what causes a job to be
  spawned. A worker that could stamp it — or that summarized a web page telling
  it to — would be authorizing its own successor onto the century's executor
  slot.
* **``ready_at`` moves only when readiness is actually asserted.** Bumping it on
  an ordinary content edit re-arms a ticket the tick already claimed, and the
  officer gets two jobs doing the same work.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.tools.knowledge.knowledge_tools import create_kb_tools

# =============================================================================
# Harness — mirrors tests/test_officer_charter.py: a session carries a
# persistent-thread id, a worker job does not, and that is the whole trust
# boundary.
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


def _ticket(tags, note_id="feature-dark-mode", content="body"):
    """A ``get_note_by_slug``-shaped backlog ticket."""
    return {
        "id": note_id,
        "title": "Dark mode",
        "type": "feature",
        "status": "active",
        "content": content,
        "confidence": None,
        "tags": list(tags),
        "keywords": [],
        "job_id": None,
        "phase": None,
        "priority": 1,
        "ready_at": None,
    }


@pytest.fixture(autouse=True)
def _no_materialization_http():
    with patch(
        "src.tools.knowledge.knowledge_tools._post_vault_file",
        return_value={"status": "skipped", "reason": "no-repo"},
    ):
        yield


def _capture_materialize():
    calls: list = []

    def _record(project_id, slug, content, job_id=None, **kw):
        calls.append({"slug": slug, "content": content})
        return {"status": "ok"}

    return (
        patch(
            "src.tools.knowledge.knowledge_tools._post_vault_file",
            side_effect=_record,
        ),
        calls,
    )


def _upsert_kwargs(ctx):
    return ctx.knowledge_store.upsert_note.call_args.kwargs


# =============================================================================
# Tag mutation — the one-way door, closed
# =============================================================================


class TestTagMutation:
    def test_remove_tags_retracts_a_tag(self):
        ctx = _session_context()
        ctx.knowledge_store.get_note_by_slug.return_value = _ticket(
            ["ready", "category:researcher"]
        )
        _tool(ctx, "kb_update").invoke(
            {"note": "feature-dark-mode", "remove_tags": ["ready"]}
        )
        assert _upsert_kwargs(ctx)["tags"] == ["category:researcher"]

    def test_set_tags_replaces_the_whole_list(self):
        ctx = _session_context()
        ctx.knowledge_store.get_note_by_slug.return_value = _ticket(
            ["ready", "category:researcher", "spike"]
        )
        _tool(ctx, "kb_update").invoke(
            {
                "note": "feature-dark-mode",
                "set_tags": ["category:executor", "expert:designer"],
            }
        )
        assert _upsert_kwargs(ctx)["tags"] == ["category:executor", "expert:designer"]

    def test_swapping_a_category_in_one_call_leaves_exactly_one(self):
        # The reason removal had to exist: a ticket matching two pools gets
        # pulled by whichever ticks first.
        ctx = _session_context()
        ctx.knowledge_store.get_note_by_slug.return_value = _ticket(
            ["ready", "category:researcher"]
        )
        _tool(ctx, "kb_update").invoke(
            {
                "note": "feature-dark-mode",
                "remove_tags": ["category:researcher"],
                "add_tags": ["category:executor"],
            }
        )
        tags = _upsert_kwargs(ctx)["tags"]
        assert [t for t in tags if t.startswith("category:")] == ["category:executor"]

    def test_add_and_remove_of_the_same_tag_resolves_to_present(self):
        # Removal runs first so an overlapping pair cannot cancel the addition.
        ctx = _session_context()
        ctx.knowledge_store.get_note_by_slug.return_value = _ticket(["spike"])
        _tool(ctx, "kb_update").invoke(
            {
                "note": "feature-dark-mode",
                "remove_tags": ["spike"],
                "add_tags": ["spike"],
            }
        )
        assert _upsert_kwargs(ctx)["tags"] == ["spike"]

    def test_set_tags_refuses_to_be_combined_with_add_or_remove(self):
        ctx = _session_context()
        ctx.knowledge_store.get_note_by_slug.return_value = _ticket([])
        result = _tool(ctx, "kb_update").invoke(
            {
                "note": "feature-dark-mode",
                "set_tags": ["a"],
                "add_tags": ["b"],
            }
        )
        assert "Error" in result
        ctx.knowledge_store.upsert_note.assert_not_called()

    def test_tags_are_lowercased_at_the_write_path(self):
        ctx = _session_context()
        ctx.knowledge_store.get_note_by_slug.return_value = _ticket([])
        _tool(ctx, "kb_update").invoke(
            {"note": "feature-dark-mode", "add_tags": ["Category:Executor", "Spike"]}
        )
        assert _upsert_kwargs(ctx)["tags"] == ["category:executor", "spike"]


# =============================================================================
# Provenance — the anti-amplification firewall
# =============================================================================


class TestOfficerOnlyTags:
    def test_worker_cannot_arm_a_ticket_it_filed(self):
        ctx = _worker_context()
        result = _tool(ctx, "kb_write").invoke(
            {
                "title": "Add dark mode",
                "type": "feature",
                "content": "the app is blinding at night",
                "tags": ["ready", "category:executor"],
            }
        )
        assert _upsert_kwargs(ctx)["tags"] == ["category:executor"]
        assert _upsert_kwargs(ctx)["ready"] is None
        # Named, not silently dropped: a worker that gets no answer keeps asking.
        assert "ignored ready" in result

    def test_worker_may_still_classify(self):
        # Classification is triage help; authorization is not. A worker filing a
        # well-tagged ticket is exactly what the backlog wants.
        ctx = _worker_context()
        _tool(ctx, "kb_write").invoke(
            {
                "title": "Add dark mode",
                "type": "feature",
                "content": "x",
                "tags": ["category:executor", "expert:designer"],
            }
        )
        assert _upsert_kwargs(ctx)["tags"] == ["category:executor", "expert:designer"]

    def test_worker_cannot_grant_itself_parallelism(self):
        ctx = _worker_context()
        ctx.knowledge_store.get_note_by_slug.return_value = _ticket(
            ["category:executor"]
        )
        _tool(ctx, "kb_update").invoke(
            {"note": "feature-dark-mode", "add_tags": ["parallel-safe"]}
        )
        assert "parallel-safe" not in _upsert_kwargs(ctx)["tags"]

    def test_worker_cannot_un_arm_a_queued_ticket(self):
        # Stripping the INPUT is not enough on its own: set_tags is absolute, so
        # a worker could drop `ready` simply by rewriting the list without it.
        ctx = _worker_context()
        ctx.knowledge_store.get_note_by_slug.return_value = _ticket(
            ["ready", "category:executor"]
        )
        _tool(ctx, "kb_update").invoke(
            {"note": "feature-dark-mode", "set_tags": ["category:researcher"]}
        )
        kwargs = _upsert_kwargs(ctx)
        assert "ready" in kwargs["tags"]
        assert kwargs["ready"] is None

    def test_worker_remove_tags_cannot_reach_the_officer_namespace(self):
        ctx = _worker_context()
        ctx.knowledge_store.get_note_by_slug.return_value = _ticket(
            ["ready", "category:executor"]
        )
        _tool(ctx, "kb_update").invoke(
            {"note": "feature-dark-mode", "remove_tags": ["ready", "category:executor"]}
        )
        kwargs = _upsert_kwargs(ctx)
        assert kwargs["tags"] == ["ready"]  # the category went, the arming stayed
        assert kwargs["ready"] is None

    def test_officer_may_do_all_of_it(self):
        ctx = _session_context()
        ctx.knowledge_store.get_note_by_slug.return_value = _ticket(
            ["category:executor"]
        )
        _tool(ctx, "kb_update").invoke(
            {"note": "feature-dark-mode", "add_tags": ["ready", "parallel-safe"]}
        )
        kwargs = _upsert_kwargs(ctx)
        assert "ready" in kwargs["tags"] and "parallel-safe" in kwargs["tags"]
        assert kwargs["ready"] is True


# =============================================================================
# ready_at — one-shot claims depend entirely on when this moves
# =============================================================================


class TestReadyAuthorization:
    def test_arming_stamps_readiness(self):
        ctx = _session_context()
        ctx.knowledge_store.get_note_by_slug.return_value = _ticket(
            ["category:executor"]
        )
        result = _tool(ctx, "kb_update").invoke(
            {"note": "feature-dark-mode", "add_tags": ["ready"]}
        )
        assert _upsert_kwargs(ctx)["ready"] is True
        assert "READY for dispatch" in result

    def test_re_arming_an_already_ready_ticket_still_stamps(self):
        # This IS the re-ready action after the officer reviews an outcome: the
        # tag list does not change, only the timestamp, and if that were treated
        # as a no-op the ticket would stay parked forever.
        ctx = _session_context()
        ctx.knowledge_store.get_note_by_slug.return_value = _ticket(
            ["ready", "category:executor"]
        )
        _tool(ctx, "kb_update").invoke(
            {"note": "feature-dark-mode", "add_tags": ["ready"]}
        )
        assert _upsert_kwargs(ctx)["ready"] is True

    def test_withdrawing_clears_readiness(self):
        ctx = _session_context()
        ctx.knowledge_store.get_note_by_slug.return_value = _ticket(
            ["ready", "category:executor"]
        )
        result = _tool(ctx, "kb_update").invoke(
            {"note": "feature-dark-mode", "remove_tags": ["ready"]}
        )
        assert _upsert_kwargs(ctx)["ready"] is False
        assert "withdrawn" in result

    def test_a_content_edit_on_a_ready_ticket_says_nothing_about_readiness(self):
        # The dangerous case. The tag list still carries `ready`, but this write
        # did not assert it — bumping ready_at here would re-arm a ticket the
        # tick already claimed and put a second job on the same work.
        ctx = _session_context()
        ctx.knowledge_store.get_note_by_slug.return_value = _ticket(
            ["ready", "category:executor"]
        )
        _tool(ctx, "kb_update").invoke(
            {"note": "feature-dark-mode", "content": "a better description"}
        )
        kwargs = _upsert_kwargs(ctx)
        assert "ready" in kwargs["tags"]
        assert kwargs["ready"] is None

    def test_a_priority_change_says_nothing_about_readiness(self):
        ctx = _session_context()
        ctx.knowledge_store.get_note_by_slug.return_value = _ticket(
            ["ready", "category:executor"]
        )
        _tool(ctx, "kb_update").invoke(
            {"note": "feature-dark-mode", "priority": "high"}
        )
        assert _upsert_kwargs(ctx)["ready"] is None

    def test_set_tags_is_absolute_and_therefore_does_assert_readiness(self):
        ctx = _session_context()
        ctx.knowledge_store.get_note_by_slug.return_value = _ticket(
            ["ready", "category:executor"]
        )
        _tool(ctx, "kb_update").invoke(
            {"note": "feature-dark-mode", "set_tags": ["category:executor"]}
        )
        assert _upsert_kwargs(ctx)["ready"] is False


# =============================================================================
# The OKF file — canonical, so it has to carry the authorization too
# =============================================================================


class TestReadyFrontmatterRoundTrip:
    def test_arming_writes_ready_at_into_the_note_file(self):
        ctx = _session_context()
        ctx.knowledge_store.get_note_by_slug.return_value = _ticket(
            ["category:executor"]
        )
        patcher, calls = _capture_materialize()
        with patcher:
            _tool(ctx, "kb_update").invoke(
                {"note": "feature-dark-mode", "add_tags": ["ready"]}
            )
        assert "ready_at:" in calls[0]["content"]

    def test_an_unrelated_edit_carries_the_existing_authorization_forward(self):
        # Every kb_update rewrites the whole file. Dropping ready_at here would
        # lose the ticket's authorization on the next full vault rebuild.
        ctx = _session_context()
        existing = _ticket(["ready", "category:executor"])
        existing["ready_at"] = "2026-08-15T09:00:00+00:00"
        ctx.knowledge_store.get_note_by_slug.return_value = existing
        patcher, calls = _capture_materialize()
        with patcher:
            _tool(ctx, "kb_update").invoke(
                {"note": "feature-dark-mode", "content": "new body"}
            )
        assert "2026-08-15T09:00:00+00:00" in calls[0]["content"]

    def test_withdrawing_removes_the_line(self):
        ctx = _session_context()
        existing = _ticket(["ready", "category:executor"])
        existing["ready_at"] = "2026-08-15T09:00:00+00:00"
        ctx.knowledge_store.get_note_by_slug.return_value = existing
        patcher, calls = _capture_materialize()
        with patcher:
            _tool(ctx, "kb_update").invoke(
                {"note": "feature-dark-mode", "remove_tags": ["ready"]}
            )
        assert "ready_at:" not in calls[0]["content"]

    def test_a_plain_note_frontmatter_is_untouched(self):
        # Same rule as priority: omitted when absent, so non-ticket notes keep
        # byte-identical frontmatter.
        ctx = _session_context()
        _tool(ctx, "kb_write")  # tools construct
        patcher, calls = _capture_materialize()
        with patcher:
            _tool(ctx, "kb_write").invoke(
                {"title": "A learning", "type": "learning", "content": "x"}
            )
        assert "ready_at:" not in calls[0]["content"]


# =============================================================================
# Search text — ticket plumbing must not rank
# =============================================================================


class TestMachineTagsStayOutOfSearch:
    @pytest.mark.asyncio
    async def test_search_doc_excludes_the_machine_namespace(self):
        # A search for "researcher" that returns every ticket in the research
        # pool buries the notes that are actually about research.
        from src.services.knowledge_store import KnowledgeStore

        db = AsyncMock()
        db.fetchval.side_effect = [None, uuid.uuid4()]  # no existing row -> INSERT
        embed = AsyncMock()
        embed.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        store = KnowledgeStore(db=db, embedding_service=embed)

        await store.upsert_note(
            note_id="feature-dark-mode",
            project_id=uuid.uuid4(),
            title="Dark mode",
            note_type="feature",
            content="the app is blinding at night",
            tags=["ready", "category:researcher", "expert:scholar", "accessibility"],
        )

        search_text = db.fetchval.call_args_list[1].args[14]  # $14 = search_doc source
        assert "accessibility" in search_text
        for machine in ("ready", "category:researcher", "expert:scholar"):
            assert machine not in search_text

    @pytest.mark.asyncio
    async def test_the_note_still_stores_every_tag(self):
        # Exclusion is about the tsvector only — the tick reads tags off the row.
        from src.services.knowledge_store import KnowledgeStore

        db = AsyncMock()
        db.fetchval.side_effect = [None, uuid.uuid4()]
        embed = AsyncMock()
        embed.embed = AsyncMock(return_value=[0.1])
        store = KnowledgeStore(db=db, embedding_service=embed)

        tags = ["ready", "category:researcher", "accessibility"]
        await store.upsert_note(
            note_id="n",
            project_id=uuid.uuid4(),
            title="T",
            note_type="feature",
            content="body",
            tags=tags,
        )
        assert db.fetchval.call_args_list[1].args[7] == tags  # $7 = tags


# =============================================================================
# The reindex path — a hand-edited file has to reach the same place
# =============================================================================


class TestReindexFrontmatter:
    def test_machine_tags_from_a_hand_edited_file_are_folded(self):
        # Without this, `Category:Executor` typed into a frontmatter block is
        # invisible to its pool: `tags @>` matches by exact string.
        from orchestrator.services.kb_reindex import note_fields

        fields = note_fields(
            "knowledge/feature-x.md",
            {
                "id": "feature-x",
                "type": "feature",
                "tags": ["Ready", "Category:Executor"],
            },
            "# Feature X",
        )
        assert fields["tags"] == ["ready", "category:executor"]

    def test_ready_at_round_trips_from_frontmatter(self):
        from orchestrator.services.kb_reindex import note_fields

        fields = note_fields(
            "knowledge/feature-x.md",
            {"id": "feature-x", "type": "feature", "ready_at": "2026-08-15T09:00:00Z"},
            "# Feature X",
        )
        assert fields["ready_at"].isoformat() == "2026-08-15T09:00:00+00:00"

    def test_absent_ready_at_means_leave_the_stored_value_alone(self):
        # A replay is not an authorization event. Falling back to now() here
        # would re-arm every ready ticket in the vault on the next reindex.
        from orchestrator.services.kb_reindex import note_fields

        fields = note_fields(
            "knowledge/feature-x.md",
            {"id": "feature-x", "type": "feature"},
            "# Feature X",
        )
        assert fields["ready_at"] is None

    def test_an_unparseable_ready_at_does_not_fail_the_row(self):
        from orchestrator.services.kb_reindex import note_fields

        fields = note_fields(
            "knowledge/feature-x.md",
            {"id": "feature-x", "type": "feature", "ready_at": "whenever"},
            "# Feature X",
        )
        assert fields["ready_at"] is None
