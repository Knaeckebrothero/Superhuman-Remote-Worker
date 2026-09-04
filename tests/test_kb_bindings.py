"""Runtime tool behavior for native + datasource-backed OKF KB bindings."""

import uuid
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.knowledge.bindings import (
    KnowledgeBinding,
    build_knowledge_bindings,
)
from src.services.knowledge_store import KnowledgeRecord
from src.shared.runtime_actor import RUNTIME_ACTOR_HEADER, RuntimeActorContext
from src.tools.knowledge.knowledge_tools import create_kb_tools


def _binding(
    alias: str,
    *,
    kind: str = "datasource",
    writable: bool = False,
    name: str | None = None,
) -> KnowledgeBinding:
    return KnowledgeBinding(
        kb_id=uuid.uuid4(),
        alias=alias,
        name=name or alias.title(),
        kind=kind,
        writable=writable,
        root_path="knowledge" if kind == "native" else "docs",
    )


def _context(bindings: list[KnowledgeBinding], *, graph=True):
    context = MagicMock()
    context.knowledge_bindings = bindings
    context.project_ids = [
        str(binding.kb_id) for binding in bindings if binding.is_native
    ]
    context.project_id = context.project_ids[0] if context.project_ids else None
    context.job_id = str(uuid.uuid4())
    context.config = {}
    context.knowledge_graph = MagicMock() if graph else None
    context.knowledge_store = AsyncMock()
    context.has_git.return_value = False
    context.has_workspace.return_value = False
    return context


def _tools(context, **kwargs):
    with patch(
        "src.tools.knowledge.knowledge_tools.asyncio.get_running_loop",
        side_effect=RuntimeError,
    ):
        return create_kb_tools(context, **kwargs)


def _tool(tools, name):
    return next(item for item in tools if item.name == name)


def _note(title: str):
    return {
        "id": "shared-note",
        "title": title,
        "type": "learning",
        "status": "active",
        "content": f"Content from {title}",
    }


def test_binding_builder_is_native_first_and_resolves_alias_collisions():
    project_id = uuid.uuid4()
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()

    bindings = build_knowledge_bindings(
        project_ids=[str(project_id)],
        datasources=[
            {
                "type": "kb",
                "datasource_id": str(first_id),
                "name": "Team Docs",
                "config": {"root_path": "vault"},
            },
            {
                "type": "kb",
                "datasource_id": str(second_id),
                "name": "Team Docs",
                "config": {"root_path": "handbook"},
            },
        ],
    )

    assert bindings[0].alias == "project"
    assert bindings[0].writable is True
    ordered_ids = sorted((first_id, second_id), key=lambda value: value.hex)
    assert bindings[1].kb_id == ordered_ids[0]
    assert bindings[2].kb_id == ordered_ids[1]
    assert bindings[1].alias == "team-docs"
    assert bindings[2].alias == f"team-docs-{ordered_ids[1].hex[:8]}"
    assert all(not binding.writable for binding in bindings[1:])


def test_runtime_actor_binds_only_to_its_exact_writable_native_project():
    primary = uuid.uuid4()
    secondary = uuid.uuid4()
    primary_actor = RuntimeActorContext(
        caller_kind="human",
        project_id=str(primary),
        project_role="owner",
        thread_id=str(uuid.uuid4()),
    )

    bindings = build_knowledge_bindings(
        project_ids=[str(primary), str(secondary)],
        runtime_actor=primary_actor,
    )
    assert bindings[0].runtime_actor is primary_actor
    assert bindings[1].runtime_actor is None

    secondary_actor = RuntimeActorContext(
        caller_kind="human",
        project_id=str(secondary),
        project_role="owner",
        thread_id=str(uuid.uuid4()),
    )
    bindings = build_knowledge_bindings(
        project_ids=[str(primary), str(secondary)],
        runtime_actor=secondary_actor,
    )
    assert all(binding.runtime_actor is None for binding in bindings)


def test_qualified_external_read_uses_store_even_when_graph_is_available():
    native = _binding("project", kind="native", writable=True)
    docs = _binding("docs", name="Product Docs")
    context = _context([native, docs])
    context.knowledge_store.get_note_by_slug.return_value = _note("Product Docs")
    context.knowledge_store.get_watermark.return_value = MagicMock(
        indexed_commit="a" * 40
    )

    result = _tool(_tools(context), "kb_read").invoke({"note": "docs:shared-note"})

    assert "Product Docs (`docs`)" in result
    assert "`docs:shared-note`" in result
    assert "External indexed snapshot" in result
    assert "[docs] @ aaaaaaaaaaaa" in result
    context.knowledge_store.get_note_by_slug.assert_awaited_once_with(
        docs.kb_id, "shared-note"
    )
    context.knowledge_graph.read_note.assert_not_called()


def test_unqualified_read_reports_ambiguity_across_bound_kbs():
    first = _binding("handbook")
    second = _binding("runbooks")
    context = _context([first, second])
    context.knowledge_store.get_note_by_slug.side_effect = [
        _note("Handbook"),
        _note("Runbooks"),
    ]

    result = _tool(_tools(context), "kb_read").invoke({"note": "shared-note"})

    assert "ambiguous" in result
    assert "handbook:shared-note" in result
    assert "runbooks:shared-note" in result


def test_kb_selector_disambiguates_unqualified_read():
    first = _binding("handbook")
    second = _binding("runbooks")
    context = _context([first, second])
    context.knowledge_store.get_note_by_slug.return_value = _note("Runbooks")

    result = _tool(_tools(context), "kb_read").invoke(
        {"note": "shared-note", "kb": "runbooks"}
    )

    assert "Runbooks" in result
    assert "runbooks:shared-note" in result
    context.knowledge_store.get_note_by_slug.assert_awaited_once_with(
        second.kb_id, "shared-note"
    )


def test_list_routes_each_binding_and_emits_qualified_handles():
    native = _binding("project", kind="native", writable=True)
    docs = _binding("docs")
    context = _context([native, docs])
    context.knowledge_graph.list_notes.return_value = [
        {"id": "native-note", "title": "Native", "type": "decision", "status": "active"}
    ]
    context.knowledge_store.list_notes.return_value = [
        {
            "id": "external-note",
            "title": "External",
            "type": "source",
            "status": "active",
        }
    ]
    context.knowledge_store.get_watermark.return_value = MagicMock(
        indexed_commit="b" * 40
    )

    result = _tool(_tools(context), "kb_list").invoke({})

    assert "project:native-note" in result
    assert "docs:external-note" in result
    assert "[docs] @ bbbbbbbbbbbb" in result
    context.knowledge_graph.list_notes.assert_called_once()
    context.knowledge_store.list_notes.assert_awaited_once()


def test_search_selector_scopes_ids_and_annotates_source():
    native = _binding("project", kind="native", writable=True)
    docs = _binding("docs")
    context = _context([native, docs])
    context.knowledge_store.embedding_service.model = "test-embedding"
    context.knowledge_store.embedding_service.expected_dimensions = 8
    context.knowledge_store.search_chunks.return_value = [
        KnowledgeRecord(
            note_id="deployments",
            kb_id=docs.kb_id,
            title="Deployments",
            note_type="source",
            content="How deployment works.",
        )
    ]
    context.knowledge_store.get_watermark.return_value = None

    result = _tool(_tools(context), "kb_search").invoke(
        {"query": "deploy", "kb": "docs"}
    )

    kwargs = context.knowledge_store.search_chunks.await_args.kwargs
    assert kwargs["kb_ids"] == [docs.kb_id]
    assert "[docs]" in result
    assert "docs:deployments" in result


def test_related_external_uses_indexed_links_and_qualified_handles():
    docs = _binding("docs")
    context = _context([docs])
    context.knowledge_store.get_note_by_slug.return_value = _note("Docs")
    context.knowledge_store.get_related_notes.return_value = [
        {
            "id": "next-note",
            "title": "Next",
            "type": "source",
            "status": "active",
            "distance": 1,
            "rel_types": ["references"],
        }
    ]
    context.knowledge_store.get_watermark.return_value = MagicMock(
        indexed_commit="c" * 40
    )

    result = _tool(_tools(context), "kb_related").invoke(
        {"note": "docs:shared-note", "max_hops": 3}
    )

    assert "docs:shared-note" in result
    assert "docs:next-note" in result
    assert "[docs] @ cccccccccccc" in result
    context.knowledge_store.get_related_notes.assert_awaited_once_with(
        kb_id=docs.kb_id, note_id="shared-note"
    )
    context.knowledge_graph.get_related.assert_not_called()


def test_external_marker_discloses_partial_convergence_not_false_snapshot():
    docs = _binding("docs")
    context = _context([docs], graph=False)
    context.knowledge_store.get_note_by_slug.return_value = _note("Docs")
    context.knowledge_store.get_watermark.return_value = MagicMock(
        indexed_commit="a" * 40,
        source_head="b" * 40,
        status="partial",
    )

    result = _tool(_tools(context), "kb_read").invoke({"note": "docs:shared-note"})

    assert "[docs] partial" in result
    assert "last clean @ aaaaaaaaaaaa" in result
    assert "source @ bbbbbbbbbbbb" in result


def test_empty_search_discloses_indexing_status_not_false_miss():
    docs = _binding("docs")
    context = _context([docs], graph=False)
    context.knowledge_store.embedding_service.model = "test-embedding"
    context.knowledge_store.embedding_service.expected_dimensions = 8
    context.knowledge_store.search_chunks.return_value = []
    context.knowledge_store.get_watermark.return_value = MagicMock(
        indexed_commit=None,
        source_head="b" * 40,
        status="indexing",
    )

    result = _tool(_tools(context), "kb_search").invoke(
        {"query": "deploy", "kb": "docs"}
    )

    assert "No knowledge notes match" in result
    assert "Still indexing" in result
    assert "[docs] indexing" in result


def test_external_only_scope_rejects_writes():
    docs = _binding("docs")
    context = _context([docs])
    tools = _tools(context)

    write_result = _tool(tools, "kb_write").invoke(
        {"title": "Nope", "type": "learning", "content": "Do not write"}
    )
    update_result = _tool(tools, "kb_update").invoke(
        {"note": "docs:shared-note", "append": "Do not write"}
    )
    index_result = _tool(tools, "kb_index").invoke({})

    assert "read-only" in write_result
    assert "read-only" in update_result
    assert "read-only" in index_result
    context.knowledge_graph.create_note.assert_not_called()
    context.knowledge_graph.update_note.assert_not_called()


def test_write_targets_only_writable_native_binding():
    native = _binding("project", kind="native", writable=True)
    docs = _binding("docs")
    context = _context([native, docs])
    context.knowledge_graph.read_note.return_value = None
    context.knowledge_graph.create_note.return_value = "native-note"
    context.knowledge_store.upsert_note.return_value = uuid.uuid4()

    with patch(
        "src.tools.knowledge.knowledge_tools._post_vault_file",
        return_value={"status": "committed", "path": "knowledge/native-note.md"},
    ):
        result = _tool(_tools(context), "kb_write").invoke(
            {"title": "Native Note", "type": "learning", "content": "Body"}
        )

    assert "native-note" in result
    assert context.knowledge_graph.create_note.call_args.kwargs["project_id"] == str(
        native.kb_id
    )


def _write(context, **extra):
    """Invoke kb_write with the vault endpoint stubbed; return (result, post)."""
    with patch(
        "src.tools.knowledge.knowledge_tools._post_vault_file",
        return_value={"status": "committed", "path": "knowledge/x.md"},
    ) as post:
        result = _tool(_tools(context), "kb_write").invoke(
            {"title": "X", "type": "learning", "content": "Body", **extra}
        )
    return result, post


def test_write_can_target_a_non_primary_native_with_kb():
    home = _binding("project", kind="native", writable=True)
    srw = _binding("project-c0d5edd4", kind="native", writable=False, name="SRW")
    context = _context([home, srw])
    context.knowledge_graph.read_note.return_value = None
    context.knowledge_graph.create_note.return_value = "x"

    result, post = _write(context, kb="project-c0d5edd4")

    assert post.call_args.args[0] == str(srw.kb_id)
    assert context.knowledge_graph.create_note.call_args.kwargs["project_id"] == str(
        srw.kb_id
    )
    assert "**project-c0d5edd4:x**" in result


def test_write_default_target_is_still_the_writable_native():
    home = _binding("project", kind="native", writable=True)
    srw = _binding("project-c0d5edd4", kind="native")
    context = _context([home, srw])
    context.knowledge_graph.read_note.return_value = None
    context.knowledge_graph.create_note.return_value = "x"

    result, post = _write(context)

    assert post.call_args.args[0] == str(home.kb_id)
    assert "**project:x**" in result


def _scoped_reader(notes: dict):
    """A ``kg.read_note`` stand-in keyed on (project_id, slug).

    Every cross-scope bug in this area looks the same: the read and the write
    disagree about which knowledge base they are in. Keying the fixture on the
    project id is what makes that visible instead of silently consistent.
    """

    def read_note(project_id, slug):
        return notes.get((str(project_id), slug))

    return read_note


def _kb_note(slug: str, content: str, **extra):
    return {
        "id": slug,
        "title": slug,
        "type": "learning",
        "status": "active",
        "content": content,
        "tags": [],
        "keywords": [],
        "relationships": [],
        **extra,
    }


def test_duplicate_repair_under_kb_never_touches_the_default_knowledge_base():
    """A byte-identical note in the target must not rewrite the default's twin."""
    home = _binding("project", kind="native", writable=True)
    srw = _binding("project-c0d5edd4", kind="native", name="SRW")
    context = _context([home, srw])
    context.knowledge_graph.read_note.side_effect = _scoped_reader(
        {
            # Identical body in the target: this is what routes kb_write into
            # the canonical-but-unsearchable repair.
            (str(srw.kb_id), "x"): _kb_note("x", "Body"),
            # The trap: a same-slug note in the default knowledge base.
            (str(home.kb_id), "x"): _kb_note("x", "Home body, must not change"),
        }
    )
    context.knowledge_store.note_is_indexed.return_value = False

    result, post = _write(context, kb="project-c0d5edd4")

    assert "Updated" in result
    assert post.call_args.args[0] == str(srw.kb_id)
    assert all(call.args[0] == str(srw.kb_id) for call in post.call_args_list), (
        post.call_args_list
    )
    assert context.knowledge_graph.update_note.call_args.kwargs["project_id"] == str(
        srw.kb_id
    )
    assert not [
        call
        for call in context.knowledge_graph.update_note.call_args_list
        if call.kwargs.get("project_id") == str(home.kb_id)
    ]


def test_supersede_retire_under_kb_retires_in_the_target_not_the_default():
    """The worst cross-scope case: a false success with a dangling link."""
    home = _binding("project", kind="native", writable=True)
    srw = _binding("project-c0d5edd4", kind="native", name="SRW")
    context = _context([home, srw])
    context.knowledge_graph.read_note.side_effect = _scoped_reader(
        {
            (str(srw.kb_id), "stale-note"): _kb_note("stale-note", "Old body"),
            # The trap: the default carries the same slug too.
            (str(home.kb_id), "stale-note"): _kb_note("stale-note", "Home body"),
        }
    )

    verdict = MagicMock()
    verdict.verdict.action = "SUPERSEDE"
    stale = MagicMock()
    stale.note_id = "stale-note"
    verdict.targets = [stale]

    with patch(
        "src.services.knowledge.ingestion.gate_candidate",
        new=AsyncMock(return_value=verdict),
    ):
        with patch(
            "src.tools.knowledge.knowledge_tools._post_vault_file",
            return_value={"status": "committed", "path": "knowledge/x.md"},
        ) as post:
            tools = _tools(
                context, verdict_service=MagicMock(), verdict_prompt="adjudicate"
            )
            result = _tool(tools, "kb_write").invoke(
                {
                    "title": "X",
                    "type": "learning",
                    "content": "Body",
                    "kb": "project-c0d5edd4",
                }
            )

    assert "superseded stale-note" in result
    assert all(call.args[0] == str(srw.kb_id) for call in post.call_args_list), (
        post.call_args_list
    )
    assert not [
        call
        for call in context.knowledge_graph.update_note.call_args_list
        if call.kwargs.get("project_id") == str(home.kb_id)
    ]
    assert context.knowledge_graph.update_note.call_args.kwargs["project_id"] == str(
        srw.kb_id
    )


def test_write_qualifies_the_no_op_line_for_an_identical_note():
    home = _binding("project", kind="native", writable=True)
    srw = _binding("project-c0d5edd4", kind="native", name="SRW")
    context = _context([home, srw])
    context.knowledge_graph.read_note.side_effect = _scoped_reader(
        {(str(srw.kb_id), "x"): _kb_note("x", "Body")}
    )
    context.knowledge_store.note_is_indexed.return_value = True

    result, post = _write(context, kb="project-c0d5edd4")

    assert "'project-c0d5edd4:x' already exists" in result
    post.assert_not_called()


def test_write_refuses_an_unknown_kb_alias_and_lists_what_is_selected():
    home = _binding("project", kind="native", writable=True)
    docs = _binding("docs")
    context = _context([home, docs])

    result, post = _write(context, kb="nope")

    assert result.startswith("Error: Knowledge base 'nope' is not selected.")
    assert "project (Project)" in result and "docs (Docs)" in result
    post.assert_not_called()


def test_write_falls_back_to_a_bare_legacy_project_id():
    """Legacy contexts set `project_id` with nothing in `project_ids`."""
    project_id = str(uuid.uuid4())
    context = MagicMock()
    context.knowledge_bindings = None
    context.project_ids = []
    context.project_id = project_id
    context.job_id = str(uuid.uuid4())
    context.config = {}
    context.knowledge_graph = MagicMock()
    context.knowledge_graph.read_note.return_value = None
    context.knowledge_store = AsyncMock()
    context.has_git.return_value = False
    context.has_workspace.return_value = False

    result, post = _write(context)

    assert post.call_args.args[0] == project_id
    # No bound scopes, so the handle stays bare — legacy output is unchanged.
    assert "**x**" in result


def test_write_refuses_external_target_and_lists_native_choices():
    home = _binding("project", kind="native", writable=True)
    docs = _binding("docs")
    context = _context([home, docs])

    result, post = _write(context, kb="docs")

    assert result.startswith("Error:") and "read-only" in result and "project" in result
    post.assert_not_called()


class _CredentialGatedPEP:
    """Fake PEP client authorizing only calls that present a runtime actor.

    The real endpoint is server-side policy; what this test pins is the
    *client* half — that a write aimed at a non-writable native binding
    presents no runtime-actor credential, so it can only be denied.
    """

    def __init__(self, *args, **kwargs):
        self.requests: list[tuple[str, dict, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def post(self, url, headers=None, json=None):
        sent = dict(headers or {})
        self.requests.append((url, sent, json))
        authorized = RUNTIME_ACTOR_HEADER in sent
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "authorized": authorized,
            "code": "authorized" if authorized else "actor_unresolved",
            "action": "machine_tags",
            "message": "ok" if authorized else "Runtime actor was denied.",
        }
        return response


def test_write_to_non_primary_target_cannot_set_officer_only_tags():
    """kb= widens the write target, never the officer authority (WP5.2)."""
    home = _binding("project", kind="native", writable=True)
    srw = replace(
        _binding("project-c0d5edd4", kind="native", name="SRW"),
        runtime_actor=RuntimeActorContext(
            caller_kind="human",
            project_id=str(uuid.uuid4()),
            project_role="owner",
            thread_id=str(uuid.uuid4()),
        ),
    )
    context = _context([home, srw])
    context.knowledge_graph.read_note.return_value = None

    pep = _CredentialGatedPEP()
    with patch("src.tools.knowledge.knowledge_tools.httpx.Client", return_value=pep):
        result, post = _write(context, kb="project-c0d5edd4", tags=["ready"])

    post.assert_not_called()
    context.knowledge_graph.create_note.assert_not_called()
    assert "Authorization denied" in result and "No changes were made" in result
    # Authority is asked for the *targeted* knowledge base, and the actor
    # rides only the writable binding — so nothing was presented.
    url, sent, payload = pep.requests[0]
    assert url.endswith("/api/runtime-actors/authorize")
    assert payload == {"action": "machine_tags", "project_id": str(srw.kb_id)}
    assert RUNTIME_ACTOR_HEADER not in sent


def test_graph_only_tools_and_export_ignore_external_bindings():
    native = _binding("project", kind="native", writable=True)
    docs = _binding("docs")
    context = _context([native, docs])
    context.knowledge_graph.get_contradictions.return_value = []
    context.knowledge_graph.get_all_notes_for_export.return_value = []
    tools = _tools(context)

    _tool(tools, "kb_contradictions").invoke({})
    _tool(tools, "kb_export").invoke({"path": "exports/kb"})

    context.knowledge_graph.get_contradictions.assert_called_once_with(
        str(native.kb_id)
    )
    context.knowledge_graph.get_all_notes_for_export.assert_called_once_with(
        str(native.kb_id)
    )
