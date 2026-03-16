"""Knowledge base tools for the Universal Agent.

Provides tools for interacting with the project knowledge base:
- Writing: kb_write, kb_update (write-through to Neo4j + pgvector)
- Reading: kb_read, kb_list, kb_search
- Graph: kb_related, kb_contradictions, kb_provenance, kb_unanswered
- Export: kb_export

These tools use the system Neo4j connection (not the external datasource
connector). Connection comes from ToolContext.knowledge_graph.

See docs/features/project_knowledge_base.md for full architecture.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from ..context import ToolContext

logger = logging.getLogger(__name__)


# Tool metadata for registry
KNOWLEDGE_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    # Write tools
    "kb_write": {
        "module": "knowledge.knowledge_tools",
        "function": "kb_write",
        "description": "Create a new knowledge note (Neo4j + pgvector write-through)",
        "category": "knowledge",
        "short_description": "Create a knowledge note in the project knowledge base.",
        "phases": ["strategic", "tactical"],
    },
    "kb_update": {
        "module": "knowledge.knowledge_tools",
        "function": "kb_update",
        "description": "Update an existing knowledge note",
        "category": "knowledge",
        "short_description": "Update a knowledge note (append, status, tags, links).",
        "phases": ["strategic", "tactical"],
    },
    # Read tools
    "kb_read": {
        "module": "knowledge.knowledge_tools",
        "function": "kb_read",
        "description": "Read a full knowledge note with metadata and relationships",
        "category": "knowledge",
        "short_description": "Read a knowledge note by slug ID.",
        "phases": ["strategic", "tactical"],
    },
    "kb_list": {
        "module": "knowledge.knowledge_tools",
        "function": "kb_list",
        "description": "List knowledge notes with optional filters",
        "category": "knowledge",
        "short_description": "List knowledge notes (filter by type, tag, status).",
        "phases": ["strategic", "tactical"],
    },
    "kb_search": {
        "module": "knowledge.knowledge_tools",
        "function": "kb_search",
        "description": "Hybrid search over knowledge base (semantic + keyword + recency)",
        "category": "knowledge",
        "short_description": "Search knowledge base with hybrid ranking.",
        "phases": ["strategic", "tactical"],
    },
    # Graph query tools
    "kb_related": {
        "module": "knowledge.knowledge_tools",
        "function": "kb_related",
        "description": "Find notes related to a given note (graph traversal)",
        "category": "knowledge",
        "short_description": "Find related notes within N hops.",
        "phases": ["strategic", "tactical"],
    },
    "kb_contradictions": {
        "module": "knowledge.knowledge_tools",
        "function": "kb_contradictions",
        "description": "Find contradicting notes in the knowledge base",
        "category": "knowledge",
        "short_description": "List notes connected by CONTRADICTS edges.",
        "phases": ["strategic", "tactical"],
    },
    "kb_provenance": {
        "module": "knowledge.knowledge_tools",
        "function": "kb_provenance",
        "description": "Trace a note's derivation chain (DERIVED_FROM)",
        "category": "knowledge",
        "short_description": "Trace DERIVED_FROM chain for a note.",
        "phases": ["strategic", "tactical"],
    },
    "kb_unanswered": {
        "module": "knowledge.knowledge_tools",
        "function": "kb_unanswered",
        "description": "List open questions with no answers",
        "category": "knowledge",
        "short_description": "List question notes without ANSWERS edges.",
        "phases": ["strategic", "tactical"],
    },
    # Export
    "kb_export": {
        "module": "knowledge.knowledge_tools",
        "function": "kb_export",
        "description": "Export knowledge base as Obsidian-compatible markdown files",
        "category": "knowledge",
        "short_description": "Export knowledge base to Obsidian .md files.",
        "phases": ["strategic"],
    },
}


def _get_project_id(context: ToolContext) -> Optional[str]:
    """Get the project ID from context."""
    return context.project_id


def create_kb_tools(context: ToolContext) -> List[Any]:
    """Create knowledge base tools with injected context.

    Args:
        context: ToolContext with knowledge_graph and knowledge_store

    Returns:
        List of LangChain tool functions
    """
    kg = context.knowledge_graph
    ks = context.knowledge_store

    # Capture the event loop at tool creation time (async graph setup).
    # Sync tools run in executor threads — they must schedule async work
    # on the original loop to preserve asyncpg connection pool affinity.
    # Using asyncio.run() from a thread would create a NEW loop, breaking
    # connections tied to the original one ("attached to a different loop").
    try:
        _creator_loop = asyncio.get_running_loop()
    except RuntimeError:
        _creator_loop = None

    def _run_async(coro):
        """Run async coroutine from sync tool context on the original event loop."""
        if _creator_loop and _creator_loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, _creator_loop)
            return future.result()
        return asyncio.run(coro)

    if not kg or not ks:
        raise ValueError("Knowledge tools require knowledge_graph and knowledge_store in ToolContext")

    # =========================================================================
    # Write Tools
    # =========================================================================

    @tool
    def kb_write(
        title: str,
        type: str,
        content: str,
        tags: Optional[List[str]] = None,
        keywords: Optional[List[str]] = None,
        confidence: Optional[str] = None,
        links: Optional[List[dict]] = None,
        retrieval_messages: Optional[List[str]] = None,
    ) -> str:
        """Create a new knowledge note in the project knowledge base.

        Write-through: creates the note in Neo4j (source of truth) AND upserts
        into pgvector search index in a single call. The note is immediately
        available for search and graph queries.

        Args:
            title: Note title (generates the slug ID, e.g. "chose-jwt-over-oauth")
            type: Note type — one of: goal, plan, decision, learning, code, source, question, state, retrospective
            content: Full markdown body of the note
            tags: List of tag names (e.g. ["authentication", "security"])
            keywords: List of keyword strings for search
            confidence: Confidence level — high, medium, or low
            links: Relationships to other notes — list of {"target": "note-slug", "type": "RELATIONSHIP_TYPE"}.
                   Types: REFERENCES, DERIVED_FROM, SUPPORTS, CONTRADICTS, ANSWERS, DEPENDS_ON, SUPERSEDES, IMPLEMENTS
            retrieval_messages: Synthetic queries describing when this note should be retrieved
                               (e.g. ["What auth approach should I use?", "Why JWT over OAuth?"])

        Returns:
            Confirmation with the note's slug ID, or error message
        """
        project_id = _get_project_id(context)
        if not project_id:
            return "Error: No project_id available. Knowledge tools require a project context."

        try:
            slug = kg.create_note(
                project_id=project_id,
                title=title,
                note_type=type,
                content=content,
                tags=tags,
                keywords=keywords,
                confidence=confidence,
                job_id=context.job_id,
                phase=context.config.get("current_phase"),
                retrieval_messages=retrieval_messages,
                links=links,
            )

            # Write-through to pgvector
            now = datetime.now(timezone.utc)
            try:
                _run_async(ks.upsert_note(
                    note_id=slug,
                    project_id=uuid.UUID(project_id),
                    title=title,
                    note_type=type,
                    content=content,
                    tags=tags,
                    keywords=keywords,
                    confidence=confidence,
                    job_id=uuid.UUID(context.job_id) if context.job_id else None,
                    phase=context.config.get("current_phase"),
                    retrieval_messages=retrieval_messages,
                    created_at=now,
                    modified_at=now,
                ))
            except Exception as e:
                logger.warning(f"pgvector write-through failed for {slug}: {e}")
                # Note exists in Neo4j — pgvector can be rebuilt

            link_info = ""
            if links:
                link_info = f", {len(links)} link(s)"

            return f"Created knowledge note: **{slug}** (type={type}{link_info})"

        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.error(f"kb_write failed: {e}")
            return f"Error creating knowledge note: {e}"

    @tool
    def kb_update(
        note: str,
        content: Optional[str] = None,
        append: Optional[str] = None,
        status: Optional[str] = None,
        confidence: Optional[str] = None,
        add_tags: Optional[List[str]] = None,
        add_links: Optional[List[dict]] = None,
    ) -> str:
        """Update an existing knowledge note.

        Write-through: updates both Neo4j and pgvector search index.

        Args:
            note: Note slug ID (e.g. "chose-jwt-over-oauth")
            content: Replace the entire content (mutually exclusive with append)
            append: Append text to existing content (mutually exclusive with content)
            status: New status — active, resolved, superseded, or archived
            confidence: New confidence level — high, medium, or low
            add_tags: Additional tags to add
            add_links: Additional relationships — list of {"target": "slug", "type": "RELATIONSHIP_TYPE"}

        Returns:
            Confirmation or error message
        """
        project_id = _get_project_id(context)
        if not project_id:
            return "Error: No project_id available."

        try:
            updated = kg.update_note(
                project_id=project_id,
                note_id=note,
                content=content,
                append=append,
                status=status,
                confidence=confidence,
                add_tags=add_tags,
                add_links=add_links,
            )

            if not updated:
                return f"Error: Note '{note}' not found in project."

            # Write-through: re-read the note from Neo4j and upsert into pgvector
            try:
                full_note = kg.read_note(project_id, note)
                if full_note:
                    _run_async(ks.upsert_note(
                        note_id=note,
                        project_id=uuid.UUID(project_id),
                        title=full_note.get("title", ""),
                        note_type=full_note.get("type", "learning"),
                        content=full_note.get("content", ""),
                        status=full_note.get("status", "active"),
                        confidence=full_note.get("confidence"),
                        tags=full_note.get("tags", []),
                        keywords=full_note.get("keywords", []),
                        job_id=uuid.UUID(full_note["job_id"]) if full_note.get("job_id") else None,
                        phase=full_note.get("phase"),
                        retrieval_messages=full_note.get("retrieval_messages", []),
                        modified_at=datetime.now(timezone.utc),
                    ))
            except Exception as e:
                logger.warning(f"pgvector write-through failed for {note}: {e}")

            changes = []
            if content is not None:
                changes.append("content replaced")
            if append is not None:
                changes.append("content appended")
            if status:
                changes.append(f"status → {status}")
            if confidence:
                changes.append(f"confidence → {confidence}")
            if add_tags:
                changes.append(f"+{len(add_tags)} tag(s)")
            if add_links:
                changes.append(f"+{len(add_links)} link(s)")

            return f"Updated **{note}**: {', '.join(changes)}"

        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.error(f"kb_update failed: {e}")
            return f"Error updating note: {e}"

    # =========================================================================
    # Read Tools
    # =========================================================================

    @tool
    def kb_read(note: str) -> str:
        """Read a full knowledge note with metadata and relationships.

        Args:
            note: Note slug ID (e.g. "chose-jwt-over-oauth")

        Returns:
            Full note content with metadata, tags, and relationships
        """
        project_id = _get_project_id(context)
        if not project_id:
            return "Error: No project_id available."

        try:
            data = kg.read_note(project_id, note)
            if not data:
                return f"Note '{note}' not found."

            # Format output
            lines = [
                f"# {data.get('title', note)}",
                "",
                f"**ID:** {data.get('id', note)}",
                f"**Type:** {data.get('type', 'unknown')}",
                f"**Status:** {data.get('status', 'unknown')}",
            ]

            if data.get("confidence"):
                lines.append(f"**Confidence:** {data['confidence']}")
            if data.get("tags"):
                lines.append(f"**Tags:** {', '.join(data['tags'])}")
            if data.get("keywords"):
                lines.append(f"**Keywords:** {', '.join(data['keywords'])}")
            if data.get("job_id"):
                lines.append(f"**Job:** {data['job_id']}")
            if data.get("phase") is not None:
                lines.append(f"**Phase:** {data['phase']}")
            if data.get("created"):
                lines.append(f"**Created:** {data['created']}")
            if data.get("modified"):
                lines.append(f"**Modified:** {data['modified']}")

            lines.extend(["", "---", "", data.get("content", "(no content)")])

            # Relationships
            rels = data.get("relationships", [])
            if rels:
                lines.extend(["", "## Outgoing Relationships"])
                for r in rels:
                    lines.append(f"- **{r['type']}** → [[{r['target']}]] ({r.get('target_title', '')})")

            incoming = data.get("incoming_relationships", [])
            if incoming:
                lines.extend(["", "## Incoming Relationships"])
                for r in incoming:
                    lines.append(f"- [[{r['source']}]] ({r.get('source_title', '')}) **{r['type']}** → this")

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"kb_read failed: {e}")
            return f"Error reading note: {e}"

    @tool
    def kb_list(
        type: Optional[str] = None,
        tag: Optional[str] = None,
        status: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> str:
        """List knowledge notes with optional filters.

        Args:
            type: Filter by note type (goal, plan, decision, learning, code, source, question, state, retrospective)
            tag: Filter by tag name
            status: Filter by status (active, resolved, superseded, archived). Default: all
            job_id: Filter by creating job UUID

        Returns:
            Formatted list of matching notes
        """
        project_id = _get_project_id(context)
        if not project_id:
            return "Error: No project_id available."

        try:
            notes = kg.list_notes(
                project_id=project_id,
                note_type=type,
                tag=tag,
                status=status,
                job_id=job_id,
            )

            if not notes:
                filters = []
                if type:
                    filters.append(f"type={type}")
                if tag:
                    filters.append(f"tag={tag}")
                if status:
                    filters.append(f"status={status}")
                filter_str = f" (filters: {', '.join(filters)})" if filters else ""
                return f"No knowledge notes found{filter_str}."

            lines = [f"**Knowledge Notes** ({len(notes)} results):", ""]

            for n in notes:
                status_icon = "●" if n.get("status") == "active" else "○"
                confidence = f" [{n['confidence']}]" if n.get("confidence") else ""
                lines.append(
                    f"{status_icon} **{n.get('id', '?')}** — {n.get('title', '(untitled)')} "
                    f"({n.get('type', '?')}{confidence})"
                )

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"kb_list failed: {e}")
            return f"Error listing notes: {e}"

    @tool
    def kb_search(query: str, max_results: int = 10) -> str:
        """Search the project knowledge base using hybrid ranking.

        Combines semantic vector search, keyword matching, and recency
        via Reciprocal Rank Fusion (RRF). Searches active notes only.

        Args:
            query: Search query (natural language or keywords)
            max_results: Maximum number of results (default 10)

        Returns:
            Ranked search results with note summaries
        """
        project_id = _get_project_id(context)
        if not project_id:
            return "Error: No project_id available."

        try:
            results = _run_async(ks.hybrid_search(
                project_id=uuid.UUID(project_id),
                query=query,
                match_count=max_results,
            ))

            if not results:
                return f"No knowledge notes match '{query}'."

            lines = [f"**Search Results** ({len(results)} matches for '{query}'):", ""]

            for i, note in enumerate(results, 1):
                meta_parts = [note.note_type]
                if note.confidence:
                    meta_parts.append(note.confidence)
                meta = ", ".join(meta_parts)

                # Truncate content for display
                preview = note.content[:200].replace("\n", " ")
                if len(note.content) > 200:
                    preview += "..."

                lines.append(f"**[{i}]** {note.note_id} — {note.title} ({meta})")
                lines.append(f"  {preview}")
                lines.append("")

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"kb_search failed: {e}")
            return f"Error searching knowledge base: {e}"

    # =========================================================================
    # Graph Query Tools
    # =========================================================================

    @tool
    def kb_related(note: str, max_hops: int = 2) -> str:
        """Find notes related to a given note via graph traversal.

        Traverses all relationship types up to max_hops edges away.

        Args:
            note: Note slug ID to find relatives of
            max_hops: Maximum relationship hops (1-3, default 2)

        Returns:
            List of related notes with relationship types and distance
        """
        project_id = _get_project_id(context)
        if not project_id:
            return "Error: No project_id available."

        try:
            results = kg.get_related(
                project_id=project_id,
                note_id=note,
                max_hops=max_hops,
            )

            if not results:
                return f"No related notes found for '{note}'."

            lines = [f"**Related to '{note}'** ({len(results)} notes):", ""]
            for r in results:
                distance = r.get("distance", "?")
                rel_types = r.get("rel_types", [])
                rel_str = " → ".join(rel_types) if rel_types else "?"
                status = f" [{r['status']}]" if r.get("status") != "active" else ""
                lines.append(
                    f"  ({distance} hop{'s' if distance != 1 else ''}) "
                    f"**{r.get('id', '?')}** — {r.get('title', '?')} "
                    f"({r.get('type', '?')}{status}) via {rel_str}"
                )

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"kb_related failed: {e}")
            return f"Error finding related notes: {e}"

    @tool
    def kb_contradictions() -> str:
        """Find all active contradiction pairs in the knowledge base.

        Returns notes connected by CONTRADICTS relationships where both
        notes are still active. Use this to identify conflicting knowledge
        that needs resolution.

        Returns:
            List of contradiction pairs
        """
        project_id = _get_project_id(context)
        if not project_id:
            return "Error: No project_id available."

        try:
            results = kg.get_contradictions(project_id)

            if not results:
                return "No active contradictions found in the knowledge base."

            lines = [f"**Active Contradictions** ({len(results)}):", ""]
            for r in results:
                lines.append(
                    f"  **{r.get('note_a', '?')}** ({r.get('title_a', '?')}) "
                    f"⟷ CONTRADICTS ⟷ "
                    f"**{r.get('note_b', '?')}** ({r.get('title_b', '?')})"
                )

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"kb_contradictions failed: {e}")
            return f"Error finding contradictions: {e}"

    @tool
    def kb_provenance(note: str) -> str:
        """Trace the derivation chain of a knowledge note.

        Follows DERIVED_FROM relationships backwards to find the original
        sources that a note was derived from.

        Args:
            note: Note slug ID to trace

        Returns:
            Ordered provenance chain from the note back to its sources
        """
        project_id = _get_project_id(context)
        if not project_id:
            return "Error: No project_id available."

        try:
            results = kg.get_provenance(project_id, note)

            if not results:
                return f"No provenance chain found for '{note}'."

            lines = [f"**Provenance of '{note}'**:", ""]
            for r in results:
                depth = r.get("depth", "?")
                lines.append(
                    f"  {'  ' * (depth - 1)}↑ **{r.get('id', '?')}** — "
                    f"{r.get('title', '?')} ({r.get('type', '?')})"
                )

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"kb_provenance failed: {e}")
            return f"Error tracing provenance: {e}"

    @tool
    def kb_unanswered() -> str:
        """List open questions that have no answers in the knowledge base.

        Returns question notes (type="question", status="active") that
        have no ANSWERS relationship pointing to or from them.

        Returns:
            List of unanswered questions
        """
        project_id = _get_project_id(context)
        if not project_id:
            return "Error: No project_id available."

        try:
            results = kg.get_unanswered(project_id)

            if not results:
                return "No unanswered questions in the knowledge base."

            lines = [f"**Unanswered Questions** ({len(results)}):", ""]
            for r in results:
                content = r.get("content", "")
                preview = content[:150].replace("\n", " ")
                if len(content) > 150:
                    preview += "..."
                lines.append(f"  ❓ **{r.get('id', '?')}** — {r.get('title', preview)}")

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"kb_unanswered failed: {e}")
            return f"Error finding unanswered questions: {e}"

    # =========================================================================
    # Export
    # =========================================================================

    @tool
    def kb_export(path: str) -> str:
        """Export the project knowledge base as Obsidian-compatible markdown files.

        Dumps all notes from Neo4j as .md files with YAML frontmatter and
        [[wikilinks]] for relationships. One-way export for human browsing.

        Args:
            path: Directory path to write the export files

        Returns:
            Summary of exported files
        """
        project_id = _get_project_id(context)
        if not project_id:
            return "Error: No project_id available."

        try:
            notes = kg.get_all_notes_for_export(project_id)
            if not notes:
                return "Knowledge base is empty — nothing to export."

            export_dir = Path(path)
            export_dir.mkdir(parents=True, exist_ok=True)

            exported = 0
            for note in notes:
                note_id = note.get("id", "unknown")
                filename = f"{note_id}.md"

                # Build YAML frontmatter
                fm_lines = ["---"]
                fm_lines.append(f"id: {note_id}")
                fm_lines.append(f"type: {note.get('type', 'unknown')}")
                if note.get("tags"):
                    fm_lines.append(f"tags: [{', '.join(note['tags'])}]")
                if note.get("keywords"):
                    fm_lines.append(f"keywords: [{', '.join(note['keywords'])}]")
                if note.get("confidence"):
                    fm_lines.append(f"confidence: {note['confidence']}")
                fm_lines.append(f"status: {note.get('status', 'active')}")
                if note.get("job_id"):
                    fm_lines.append(f"job_id: {note['job_id']}")
                if note.get("phase") is not None:
                    fm_lines.append(f"phase: {note['phase']}")
                if note.get("created"):
                    fm_lines.append(f"created: {note['created']}")
                if note.get("modified"):
                    fm_lines.append(f"modified: {note['modified']}")
                fm_lines.append("---")
                fm_lines.append("")

                # Title and content
                title = note.get("title", note_id)
                content = note.get("content", "")

                body_lines = [f"# {title}", "", content]

                # Relationships as wikilinks
                rels = note.get("relationships", [])
                if rels:
                    body_lines.extend(["", "## Relationships"])
                    # Group by type
                    by_type: Dict[str, List[str]] = {}
                    for r in rels:
                        rt = r.get("type", "REFERENCES")
                        by_type.setdefault(rt, []).append(r.get("target", "?"))
                    for rel_type, targets in by_type.items():
                        links = ", ".join(f"[[{t}]]" for t in targets)
                        body_lines.append(f"**{rel_type}:** {links}")

                file_content = "\n".join(fm_lines) + "\n".join(body_lines) + "\n"
                (export_dir / filename).write_text(file_content)
                exported += 1

            return (
                f"Exported {exported} note(s) to `{path}/`.\n"
                f"Open in Obsidian or any markdown viewer."
            )

        except Exception as e:
            logger.error(f"kb_export failed: {e}")
            return f"Error exporting knowledge base: {e}"

    # =========================================================================
    # Return all tools
    # =========================================================================

    return [
        kb_write,
        kb_update,
        kb_read,
        kb_list,
        kb_search,
        kb_related,
        kb_contradictions,
        kb_provenance,
        kb_unanswered,
        kb_export,
    ]
