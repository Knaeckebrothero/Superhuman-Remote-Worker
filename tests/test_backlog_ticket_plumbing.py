"""Backlog ticket plumbing — B2 of knowledge-base/knowledge/features/officer_backlog_pools.md.

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
from src.services.knowledge.bindings import KnowledgeBinding
from src.shared.runtime_actor import (
    SENSITIVE_KNOWLEDGE_HUMAN_ROLE_POLICY,
    RuntimeActorContext,
    RuntimeAuthorizationResult,
)

# =============================================================================
# Harness: authority comes from the server-derived actor carried by the exact
# writable binding. A thread id by itself has no standing.
# =============================================================================


def _session_context(
    thread_id="11111111-2222-3333-4444-555555555555",
    *,
    caller_kind="human",
    project_role="owner",
):
    ctx = MagicMock()
    ctx.project_id = str(uuid.uuid4())
    ctx.project_ids = [ctx.project_id]
    ctx.job_id = ctx.project_id
    ctx.config = {"current_phase": None}
    ctx.knowledge_graph = None
    ctx.knowledge_store = AsyncMock()
    actor = RuntimeActorContext(
        caller_kind=caller_kind,
        project_id=ctx.project_id,
        project_role=project_role,
        thread_id=thread_id,
        officer_incarnation=0 if caller_kind == "officer" else None,
        user_id=str(uuid.uuid4()),
    )
    ctx.runtime_actor = actor
    ctx.knowledge_bindings = [
        KnowledgeBinding(
            kb_id=uuid.UUID(ctx.project_id),
            alias="project",
            name="Project Knowledge",
            kind="native",
            writable=True,
            runtime_actor=actor,
        )
    ]
    ctx._thread_id = thread_id
    ctx.has_git = MagicMock(return_value=False)
    return ctx


def _worker_context():
    ctx = _session_context(thread_id=None, caller_kind="worker", project_role=None)
    ctx.job_id = str(uuid.uuid4())
    return ctx


def _officer_context():
    return _session_context(caller_kind="officer", project_role="owner")


def _authorize_from_test_actor(ctx, project_id, action):
    actor = ctx.runtime_actor
    allowed = bool(
        actor.project_id == project_id
        and (
            actor.caller_kind == "officer"
            or (
                actor.caller_kind in {"human", "conference"}
                and SENSITIVE_KNOWLEDGE_HUMAN_ROLE_POLICY.get(
                    actor.project_role or "", False
                )
            )
        )
    )
    return RuntimeAuthorizationResult(
        authorized=allowed,
        code="authorized" if allowed else "project_role_denied",
        action=action,
        actor=actor.audit_payload(),
        message="allowed by test PEP" if allowed else "role is denied by policy",
    )


def _tool(ctx, name):
    for t in create_kb_tools(ctx):
        if t.name == name:
            return t
    raise KeyError(name)


_PRIOR_READY_AT = "2026-08-15T09:00:00+00:00"


def _ticket(tags, note_id="feature-dark-mode", content="body"):
    """A ``get_note_by_slug``-shaped backlog ticket.

    A ticket the store says is ``ready`` necessarily carries the stamp that
    armed it. Modelling that is what makes "this write said nothing about
    readiness" distinguishable from "this write withdrew it" in the file —
    the two render identically for a ticket that was never armed.
    """
    tags = list(tags)
    return {
        "id": note_id,
        "title": "Dark mode",
        "type": "feature",
        "status": "active",
        "content": content,
        "confidence": None,
        "tags": tags,
        "keywords": [],
        "job_id": None,
        "phase": None,
        "priority": 1,
        "ready_at": _PRIOR_READY_AT if "ready" in tags else None,
    }


@pytest.fixture(autouse=True)
def canonical_writes():
    """Every note handed to the canonical write, in order.

    Keeps the module off the network, and doubles as the observation point:
    since Slice A the OKF file is kb_write/kb_update's *only* write — the
    materialisation endpoint owns the ``knowledge_index`` row it indexes from
    that commit — so ``upsert_note`` is never called and asserting on it
    would assert nothing.
    """
    calls: list = []

    def _record(project_id, slug, content, job_id=None, **kw):
        calls.append({"slug": slug, "content": content})
        return {
            "status": "committed",
            "canonical_state": "canonical",
            "projection_state": "pending",
            "retry_state": "none",
            "indexed": True,
        }

    with (
        patch(
            "src.tools.knowledge.knowledge_tools._post_vault_file",
            side_effect=_record,
        ),
        patch(
            "src.tools.knowledge.knowledge_tools._request_runtime_actor_authorization",
            side_effect=_authorize_from_test_actor,
        ),
    ):
        yield calls


def _capture_materialize():
    calls: list = []

    def _record(project_id, slug, content, job_id=None, **kw):
        calls.append({"slug": slug, "content": content})
        return {
            "status": "committed",
            "canonical_state": "canonical",
            "projection_state": "pending",
            "retry_state": "none",
        }

    return (
        patch(
            "src.tools.knowledge.knowledge_tools._post_vault_file",
            side_effect=_record,
        ),
        calls,
    )


def _canonical_fields(calls):
    """The last note written, parsed back by the reindexer's own mapper.

    Strictly stronger than the ``upsert_note`` kwargs this used to read: these
    are the exact bytes the orchestrator commits, run through the code that
    turns them into the row a pool query reads.
    """
    from orchestrator.services.kb_reindex import note_fields, parse_note_md

    entry = calls[-1]
    fm, body = parse_note_md(entry["content"])
    return note_fields(f"knowledge/{entry['slug']}.md", fm, body)


def _ready_outcome(calls):
    """What the written file says happened to the dispatch authorization.

    ``ready`` was a tri-state *argument* of the row write Slice A deleted.
    The file — which is what a vault rebuild reads, and now the only carrier —
    says the same three things: a **fresh** ``ready_at:`` means this write
    armed the ticket, **no line** means it withdrew the authorization, and
    the **prior stamp verbatim** means it said nothing about readiness.

    Assumes the note started from ``_ticket``'s fixed ``_PRIOR_READY_AT``.
    A ticket that was never armed cannot distinguish "said nothing" from
    "withdrew" — both render no line — which is a true property of the
    canonical artefact, not a gap in this helper.
    """
    ready_at = _canonical_fields(calls)["ready_at"]
    if ready_at is None:
        return False
    if ready_at.isoformat() == _PRIOR_READY_AT:
        return None
    return True


# =============================================================================
# Tag mutation — the one-way door, closed
# =============================================================================


class TestTagMutation:
    def test_remove_tags_retracts_a_tag(self, canonical_writes):
        ctx = _session_context()
        ctx.knowledge_store.get_note_by_slug.return_value = _ticket(
            ["ready", "category:researcher"]
        )
        _tool(ctx, "kb_update").invoke(
            {"note": "feature-dark-mode", "remove_tags": ["ready"]}
        )
        assert _canonical_fields(canonical_writes)["tags"] == ["category:researcher"]

    def test_set_tags_replaces_the_whole_list(self, canonical_writes):
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
        assert _canonical_fields(canonical_writes)["tags"] == [
            "category:executor",
            "expert:designer",
        ]

    def test_swapping_a_category_in_one_call_leaves_exactly_one(self, canonical_writes):
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
        tags = _canonical_fields(canonical_writes)["tags"]
        assert [t for t in tags if t.startswith("category:")] == ["category:executor"]

    def test_add_and_remove_of_the_same_tag_resolves_to_present(self, canonical_writes):
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
        assert _canonical_fields(canonical_writes)["tags"] == ["spike"]

    def test_set_tags_refuses_to_be_combined_with_add_or_remove(self, canonical_writes):
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
        assert canonical_writes == []

    def test_tags_are_lowercased_at_the_write_path(self, canonical_writes):
        ctx = _session_context()
        ctx.knowledge_store.get_note_by_slug.return_value = _ticket([])
        _tool(ctx, "kb_update").invoke(
            {"note": "feature-dark-mode", "add_tags": ["Category:Executor", "Spike"]}
        )
        assert _canonical_fields(canonical_writes)["tags"] == [
            "category:executor",
            "spike",
        ]


# =============================================================================
# Provenance — the anti-amplification firewall
# =============================================================================


class TestOfficerOnlyTags:
    def test_worker_cannot_arm_a_ticket_it_filed(self, canonical_writes):
        ctx = _worker_context()
        result = _tool(ctx, "kb_write").invoke(
            {
                "title": "Add dark mode",
                "type": "feature",
                "content": "the app is blinding at night",
                "tags": ["ready", "category:executor"],
            }
        )
        assert canonical_writes == []
        assert "Authorization denied" in result
        assert "No changes were made" in result
        assert "actor=worker" in result

    def test_worker_may_still_classify(self):
        # Classification is triage help; authorization is not. A worker filing a
        # well-tagged ticket is exactly what the backlog wants. kb_write's only
        # write is the canonical note now, so that is where the tags must land.
        ctx = _worker_context()
        patcher, calls = _capture_materialize()
        with patcher:
            _tool(ctx, "kb_write").invoke(
                {
                    "title": "Add dark mode",
                    "type": "feature",
                    "content": "x",
                    "tags": ["category:executor", "expert:designer"],
                }
            )
        assert len(calls) == 1
        assert 'tags: ["category:executor", "expert:designer"]' in calls[0]["content"]

    def test_worker_cannot_grant_itself_parallelism(self, canonical_writes):
        ctx = _worker_context()
        ctx.knowledge_store.get_note_by_slug.return_value = _ticket(
            ["category:executor"]
        )
        result = _tool(ctx, "kb_update").invoke(
            {"note": "feature-dark-mode", "add_tags": ["parallel-safe"]}
        )
        assert canonical_writes == []
        assert "No changes were made" in result

    def test_worker_cannot_un_arm_a_queued_ticket(self, canonical_writes):
        # Stripping the INPUT is not enough on its own: set_tags is absolute, so
        # a worker could drop `ready` simply by rewriting the list without it.
        ctx = _worker_context()
        ctx.knowledge_store.get_note_by_slug.return_value = _ticket(
            ["ready", "category:executor"]
        )
        result = _tool(ctx, "kb_update").invoke(
            {"note": "feature-dark-mode", "set_tags": ["category:researcher"]}
        )
        assert canonical_writes == []
        assert "No changes were made" in result

    def test_worker_remove_tags_cannot_reach_the_officer_namespace(
        self, canonical_writes
    ):
        ctx = _worker_context()
        ctx.knowledge_store.get_note_by_slug.return_value = _ticket(
            ["ready", "category:executor"]
        )
        result = _tool(ctx, "kb_update").invoke(
            {"note": "feature-dark-mode", "remove_tags": ["ready", "category:executor"]}
        )
        assert canonical_writes == []
        assert "No changes were made" in result

    def test_officer_may_do_all_of_it(self, canonical_writes):
        ctx = _officer_context()
        ctx.knowledge_store.get_note_by_slug.return_value = _ticket(
            ["category:executor"]
        )
        _tool(ctx, "kb_update").invoke(
            {"note": "feature-dark-mode", "add_tags": ["ready", "parallel-safe"]}
        )
        tags = _canonical_fields(canonical_writes)["tags"]
        assert "ready" in tags and "parallel-safe" in tags
        assert _ready_outcome(canonical_writes) is True

    @pytest.mark.parametrize(
        ("caller_kind", "project_role", "allowed"),
        [
            ("worker", None, False),
            ("human", "viewer", False),
            ("human", "editor", False),
            ("human", "owner", True),
            ("human", "admin", True),
            ("officer", "owner", True),
            ("conference", "viewer", False),
            ("conference", "editor", False),
            ("conference", "owner", True),
            ("conference", "admin", True),
        ],
    )
    @pytest.mark.parametrize(
        ("initial_tags", "mutation", "expected_ready"),
        [
            (["category:executor"], {"add_tags": ["ready"]}, True),
            (["ready", "category:executor"], {"remove_tags": ["ready"]}, False),
            (["ready", "category:executor"], {"add_tags": ["ready"]}, True),
            # Starts armed so "said nothing about readiness" is observable in
            # the file at all: for a never-armed ticket, saying nothing and
            # withdrawing both render no `ready_at:` line.
            (["ready", "category:executor"], {"add_tags": ["parallel-safe"]}, None),
        ],
        ids=["ready", "remove-ready", "re-ready", "parallel-safe"],
    )
    def test_sensitive_tag_human_role_matrix(
        self,
        caller_kind,
        project_role,
        allowed,
        initial_tags,
        mutation,
        expected_ready,
        canonical_writes,
    ):
        ctx = _session_context(
            thread_id=None if caller_kind == "worker" else "thread-1",
            caller_kind=caller_kind,
            project_role=project_role,
        )
        ctx.knowledge_store.get_note_by_slug.return_value = _ticket(initial_tags)
        result = _tool(ctx, "kb_update").invoke(
            {"note": "feature-dark-mode", **mutation}
        )
        if allowed:
            assert _ready_outcome(canonical_writes) is expected_ready
        else:
            assert canonical_writes == []
            assert "No changes were made" in result

    def test_denied_machine_tag_write_has_zero_projection_or_file_side_effects(self):
        ctx = _session_context(caller_kind="human", project_role="viewer")
        existing = _ticket(["category:executor"])
        before = {**existing, "tags": list(existing["tags"])}
        ctx.knowledge_graph = MagicMock()
        ctx.knowledge_graph.read_note.return_value = existing
        with patch(
            "src.tools.knowledge.knowledge_tools._post_vault_file"
        ) as canonical_write:
            result = _tool(ctx, "kb_update").invoke(
                {
                    "note": "feature-dark-mode",
                    "content": "also change the body",
                    "add_tags": ["ready", "parallel-safe"],
                }
            )

        assert "Authorization denied" in result
        assert "No changes were made" in result
        assert existing == before  # tags + ready_at remain byte-for-byte intent
        ctx.knowledge_graph.update_note.assert_not_called()
        canonical_write.assert_not_called()


# =============================================================================
# ready_at — one-shot claims depend entirely on when this moves
# =============================================================================


class TestReadyAuthorization:
    def test_arming_stamps_readiness(self, canonical_writes):
        ctx = _session_context()
        ctx.knowledge_store.get_note_by_slug.return_value = _ticket(
            ["category:executor"]
        )
        result = _tool(ctx, "kb_update").invoke(
            {"note": "feature-dark-mode", "add_tags": ["ready"]}
        )
        assert _ready_outcome(canonical_writes) is True
        assert "READY for dispatch" in result

    def test_re_arming_an_already_ready_ticket_still_stamps(self, canonical_writes):
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
        # The stamp has to *move*: carrying the old one forward would leave
        # the ticket parked exactly as it was.
        assert _ready_outcome(canonical_writes) is True

    def test_withdrawing_clears_readiness(self, canonical_writes):
        ctx = _session_context()
        ctx.knowledge_store.get_note_by_slug.return_value = _ticket(
            ["ready", "category:executor"]
        )
        result = _tool(ctx, "kb_update").invoke(
            {"note": "feature-dark-mode", "remove_tags": ["ready"]}
        )
        assert _ready_outcome(canonical_writes) is False
        assert "withdrawn" in result

    def test_a_content_edit_on_a_ready_ticket_says_nothing_about_readiness(
        self, canonical_writes
    ):
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
        assert "ready" in _canonical_fields(canonical_writes)["tags"]
        assert _ready_outcome(canonical_writes) is None

    def test_a_priority_change_says_nothing_about_readiness(self, canonical_writes):
        ctx = _session_context()
        ctx.knowledge_store.get_note_by_slug.return_value = _ticket(
            ["ready", "category:executor"]
        )
        _tool(ctx, "kb_update").invoke(
            {"note": "feature-dark-mode", "priority": "high"}
        )
        assert _ready_outcome(canonical_writes) is None

    def test_set_tags_is_absolute_and_therefore_does_assert_readiness(
        self, canonical_writes
    ):
        ctx = _session_context()
        ctx.knowledge_store.get_note_by_slug.return_value = _ticket(
            ["ready", "category:executor"]
        )
        _tool(ctx, "kb_update").invoke(
            {"note": "feature-dark-mode", "set_tags": ["category:executor"]}
        )
        assert _ready_outcome(canonical_writes) is False


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
