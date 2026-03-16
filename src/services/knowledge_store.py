"""KnowledgeStore — pgvector search index for project knowledge base.

Write-through companion to KnowledgeGraphDB. Every kb_write/kb_update
call writes to Neo4j (source of truth) first, then upserts into this
search index. Never edited directly — rebuilt from Neo4j if needed.

See docs/features/project_knowledge_base.md for full architecture.

Usage:
    ```python
    from src.services.knowledge_store import KnowledgeStore
    from src.services.embedding_service import get_embedding_service

    store = KnowledgeStore(
        db=postgres_db,
        embedding_service=get_embedding_service(),
    )

    # Write-through after Neo4j write
    await store.upsert_note(
        note_id="chose-jwt-over-oauth",
        project_id=project_uuid,
        title="Chose JWT over OAuth",
        note_type="decision",
        content="After evaluating both...",
        tags=["authentication"],
        keywords=["JWT", "OAuth"],
        retrieval_messages=["What auth approach?"],
    )

    # Hybrid search
    results = await store.hybrid_search(
        project_id=project_uuid,
        query="authentication approach",
    )
    ```
"""

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


@dataclass
class KnowledgeRecord:
    """A single knowledge note from the search index."""

    id: Optional[uuid.UUID] = None
    note_id: str = ""
    project_id: Optional[uuid.UUID] = None
    title: str = ""
    note_type: str = ""
    status: str = "active"
    confidence: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    job_id: Optional[uuid.UUID] = None
    phase: Optional[int] = None
    content: str = ""
    retrieval_messages: List[str] = field(default_factory=list)
    created_at: Optional[datetime] = None
    modified_at: Optional[datetime] = None
    indexed_at: Optional[datetime] = None
    content_hash: Optional[str] = None

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "KnowledgeRecord":
        """Create a KnowledgeRecord from a database row dict."""
        return cls(
            id=row.get("id"),
            note_id=row.get("note_id", ""),
            project_id=row.get("project_id"),
            title=row.get("title", ""),
            note_type=row.get("note_type", ""),
            status=row.get("status", "active"),
            confidence=row.get("confidence"),
            tags=row.get("tags") or [],
            keywords=row.get("keywords") or [],
            job_id=row.get("job_id"),
            phase=row.get("phase"),
            content=row.get("content", ""),
            retrieval_messages=row.get("retrieval_messages") or [],
            created_at=row.get("created_at"),
            modified_at=row.get("modified_at"),
            indexed_at=row.get("indexed_at"),
            content_hash=row.get("content_hash"),
        )


class KnowledgeStore:
    """pgvector search index for project knowledge base.

    Provides write-through upserts and hybrid search (RRF over
    dense vector + sparse keyword + recency).
    """

    def __init__(self, db, embedding_service):
        """Initialize KnowledgeStore.

        Args:
            db: PostgresDB instance (async, with execute/fetch/fetchval/fetchrow)
            embedding_service: EmbeddingService for generating vectors
        """
        self.db = db
        self.embedding_service = embedding_service

    @staticmethod
    def _content_hash(content: str) -> str:
        """SHA-256 hash of content for change detection."""
        return hashlib.sha256(content.encode()).hexdigest()

    @staticmethod
    def _prepare_embedding(embedding: Union[List[float], str]) -> List[float]:
        """Prepare embedding for asyncpg with pgvector codec.

        The pgvector asyncpg codec (registered via register_vector on
        connection init) expects List[float] or numpy arrays — not strings.
        If a string is passed (legacy), convert it back to a list.
        """
        if isinstance(embedding, list):
            return embedding
        if isinstance(embedding, str):
            # Legacy string format "[0.1,0.2,...]" — parse back to list
            cleaned = embedding.strip("[]")
            return [float(v) for v in cleaned.split(",") if v.strip()]
        return list(embedding)

    async def upsert_note(
        self,
        note_id: str,
        project_id: uuid.UUID,
        title: str,
        note_type: str,
        content: str,
        status: str = "active",
        confidence: Optional[str] = None,
        tags: Optional[List[str]] = None,
        keywords: Optional[List[str]] = None,
        job_id: Optional[uuid.UUID] = None,
        phase: Optional[int] = None,
        retrieval_messages: Optional[List[str]] = None,
        created_at: Optional[datetime] = None,
        modified_at: Optional[datetime] = None,
    ) -> uuid.UUID:
        """Upsert a note into the search index (write-through from Neo4j).

        Generates embedding only if content has changed (checked via content_hash).

        Args:
            note_id: Note slug from Neo4j
            project_id: Project UUID
            title: Note title
            note_type: Note type
            content: Full note body
            status: Note status
            confidence: Confidence level
            tags: Tag list
            keywords: Keyword list
            job_id: Creating job UUID
            phase: Phase number
            retrieval_messages: Retrieval query strings
            created_at: Original creation timestamp
            modified_at: Last modification timestamp

        Returns:
            UUID of the upserted row
        """
        new_hash = self._content_hash(content)
        tags_list = tags or []
        keywords_list = keywords or []
        retrieval_list = retrieval_messages or []

        # Check if content changed (skip re-embedding for metadata-only updates)
        existing_hash = await self.db.fetchval(
            """
            SELECT content_hash FROM knowledge_index
            WHERE project_id = $1 AND note_id = $2
            """,
            project_id,
            note_id,
        )

        if existing_hash == new_hash:
            # Content unchanged — update metadata only, skip embedding
            row_id = await self.db.fetchval(
                """
                UPDATE knowledge_index
                SET title = $3, note_type = $4, status = $5, confidence = $6,
                    tags = $7, keywords = $8, job_id = $9, phase = $10,
                    retrieval_messages = $11, modified_at = $12,
                    indexed_at = NOW()
                WHERE project_id = $1 AND note_id = $2
                RETURNING id
                """,
                project_id, note_id, title, note_type, status, confidence,
                tags_list, keywords_list, job_id, phase,
                retrieval_list, modified_at,
            )
            logger.debug(f"Updated knowledge index (metadata only): {note_id}")
            return row_id

        # Content changed — generate new embedding
        # Embed content + retrieval messages together for better recall
        embed_text = content
        if retrieval_list:
            embed_text += "\n\n" + "\n".join(retrieval_list)

        embedding_raw = await self.embedding_service.embed(embed_text)
        embedding_vec = self._prepare_embedding(embedding_raw)

        # Build tsvector text from all searchable fields
        search_text = " ".join([
            title,
            content,
            " ".join(tags_list),
            " ".join(keywords_list),
            " ".join(retrieval_list),
        ])

        row_id = await self.db.fetchval(
            """
            INSERT INTO knowledge_index (
                note_id, project_id, title, note_type, status, confidence,
                tags, keywords, job_id, phase, content, retrieval_messages,
                embedding, search_doc, created_at, modified_at, indexed_at,
                content_hash
            ) VALUES (
                $1, $2, $3, $4, $5, $6,
                $7, $8, $9, $10, $11, $12,
                $13, to_tsvector('english', $14), $15, $16, NOW(),
                $17
            )
            ON CONFLICT (project_id, note_id) DO UPDATE SET
                title = EXCLUDED.title,
                note_type = EXCLUDED.note_type,
                status = EXCLUDED.status,
                confidence = EXCLUDED.confidence,
                tags = EXCLUDED.tags,
                keywords = EXCLUDED.keywords,
                job_id = EXCLUDED.job_id,
                phase = EXCLUDED.phase,
                content = EXCLUDED.content,
                retrieval_messages = EXCLUDED.retrieval_messages,
                embedding = EXCLUDED.embedding,
                search_doc = EXCLUDED.search_doc,
                modified_at = EXCLUDED.modified_at,
                indexed_at = NOW(),
                content_hash = EXCLUDED.content_hash
            RETURNING id
            """,
            note_id, project_id, title, note_type, status, confidence,
            tags_list, keywords_list, job_id, phase, content, retrieval_list,
            embedding_vec, search_text, created_at, modified_at,
            new_hash,
        )

        logger.debug(f"Upserted knowledge index: {note_id} (content changed)")
        return row_id

    async def delete_note(self, project_id: uuid.UUID, note_id: str) -> bool:
        """Delete a note from the search index.

        Args:
            project_id: Project UUID
            note_id: Note slug

        Returns:
            True if a row was deleted
        """
        result = await self.db.fetchval(
            """
            DELETE FROM knowledge_index
            WHERE project_id = $1 AND note_id = $2
            RETURNING id
            """,
            project_id,
            note_id,
        )
        return result is not None

    # =========================================================================
    # Search
    # =========================================================================

    async def hybrid_search(
        self,
        project_id: uuid.UUID,
        query: str,
        match_count: int = 10,
        dense_weight: float = 0.6,
        sparse_weight: float = 0.3,
        recency_weight: float = 0.1,
    ) -> List[KnowledgeRecord]:
        """Execute hybrid search (RRF over dense + sparse + recency).

        Args:
            project_id: Project UUID
            query: Search query text
            match_count: Max results
            dense_weight: Weight for vector similarity
            sparse_weight: Weight for keyword match
            recency_weight: Weight for recency

        Returns:
            List of KnowledgeRecord objects ranked by RRF score
        """
        query_embedding = await self.embedding_service.embed(query)

        rows = await self.db.fetch(
            """
            SELECT * FROM knowledge_hybrid_search(
                $1, $2, $3, $4, $5, $6, $7
            )
            """,
            query,
            query_embedding,
            project_id,
            match_count,
            dense_weight,
            sparse_weight,
            recency_weight,
        )

        return [KnowledgeRecord.from_row(dict(row)) for row in rows]

    async def get_summary(self, project_id: uuid.UUID) -> Dict[str, Any]:
        """Get knowledge base summary for context injection.

        Args:
            project_id: Project UUID

        Returns:
            Dict with note counts by type, total count, recent notes
        """
        row = await self.db.fetchrow(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE status = 'active') AS active,
                COUNT(*) FILTER (WHERE note_type = 'decision') AS decisions,
                COUNT(*) FILTER (WHERE note_type = 'learning') AS learnings,
                COUNT(*) FILTER (WHERE note_type = 'question' AND status = 'active') AS open_questions,
                COUNT(*) FILTER (WHERE note_type = 'goal') AS goals,
                COUNT(*) FILTER (WHERE note_type = 'code') AS code_notes,
                COUNT(*) FILTER (WHERE note_type = 'state') AS state_notes,
                MAX(modified_at) AS last_modified
            FROM knowledge_index
            WHERE project_id = $1
            """,
            project_id,
        )

        summary = dict(row) if row else {"total": 0}

        # Get 5 most recent notes
        recent = await self.db.fetch(
            """
            SELECT note_id, title, note_type, status, modified_at
            FROM knowledge_index
            WHERE project_id = $1 AND status = 'active'
            ORDER BY modified_at DESC
            LIMIT 5
            """,
            project_id,
        )
        summary["recent_notes"] = [dict(r) for r in recent]

        return summary

    # =========================================================================
    # Rebuild (Recovery)
    # =========================================================================

    async def rebuild_from_notes(
        self,
        project_id: uuid.UUID,
        notes: List[Dict[str, Any]],
    ) -> int:
        """Rebuild the search index from Neo4j note data.

        Used for cold start or recovery when pgvector drifts from Neo4j.

        Args:
            project_id: Project UUID
            notes: List of note dicts from KnowledgeGraphDB.get_all_notes_for_export()

        Returns:
            Number of notes indexed
        """
        # Clear existing index for this project
        await self.db.execute(
            "DELETE FROM knowledge_index WHERE project_id = $1",
            project_id,
        )

        count = 0
        for note in notes:
            try:
                # Convert Neo4j DateTime to Python datetime if needed
                created = note.get("created")
                modified = note.get("modified")
                if hasattr(created, 'to_native'):
                    created = created.to_native()
                if hasattr(modified, 'to_native'):
                    modified = modified.to_native()

                await self.upsert_note(
                    note_id=note.get("id", ""),
                    project_id=project_id,
                    title=note.get("title", ""),
                    note_type=note.get("type", "learning"),
                    content=note.get("content", ""),
                    status=note.get("status", "active"),
                    confidence=note.get("confidence"),
                    tags=note.get("tags", []),
                    keywords=note.get("keywords", []),
                    job_id=uuid.UUID(note["job_id"]) if note.get("job_id") else None,
                    phase=note.get("phase"),
                    retrieval_messages=note.get("retrieval_messages", []),
                    created_at=created,
                    modified_at=modified,
                )
                count += 1
            except Exception as e:
                logger.warning(f"Failed to index note {note.get('id')}: {e}")

        logger.info(f"Rebuilt knowledge index for project {project_id}: {count} notes")
        return count

    # =========================================================================
    # Formatting
    # =========================================================================

    @staticmethod
    def format_note(note: KnowledgeRecord, index: int) -> str:
        """Format a single note for context injection.

        Args:
            note: KnowledgeRecord to format
            index: Display index (1-based)

        Returns:
            Formatted note string
        """
        meta_parts = [note.note_type]
        if note.confidence:
            meta_parts.append(f"{note.confidence} confidence")
        if note.phase is not None:
            meta_parts.append(f"phase {note.phase}")

        meta = ", ".join(meta_parts)
        links = ""
        if note.tags:
            links = " Tags: " + ", ".join(note.tags)

        # Truncate content for injection
        content = note.content
        if len(content) > 500:
            content = content[:497] + "..."

        return f"[{index}] ({meta}){links}\n{content}"

    @classmethod
    def assemble_knowledge_block(
        cls,
        notes: List[KnowledgeRecord],
    ) -> str:
        """Assemble formatted knowledge block for context injection.

        Args:
            notes: List of KnowledgeRecord objects

        Returns:
            Formatted knowledge block string
        """
        if not notes:
            return ""

        lines = ["--- Project Knowledge ---", ""]
        for i, note in enumerate(notes, 1):
            lines.append(cls.format_note(note, i))
            lines.append("")

        tokens_est = sum(len(n.content) // 4 for n in notes)
        lines.append(
            f"--- End Knowledge ({len(notes)} notes, ~{tokens_est:,} tokens) ---"
        )
        return "\n".join(lines)


__all__ = ["KnowledgeStore", "KnowledgeRecord"]
