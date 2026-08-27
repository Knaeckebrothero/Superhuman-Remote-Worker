"""Runtime tool behavior for native + datasource-backed OKF KB bindings."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.knowledge.bindings import (
    KnowledgeBinding,
    build_knowledge_bindings,
)
from src.services.knowledge_store import KnowledgeRecord
from src.shared.runtime_actor import RuntimeActorContext
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


def _tools(context):
    with patch(
        "src.tools.knowledge.knowledge_tools.asyncio.get_running_loop",
        side_effect=RuntimeError,
    ):
        return create_kb_tools(context)


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
