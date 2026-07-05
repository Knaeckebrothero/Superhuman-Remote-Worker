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
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)


# TTL (in loop cycles) by note_type for KB convergence — see
# docs/features/kb_convergence_ttl_reverification.md. ``None`` = no TTL (durable
# facts never expire on a clock; they're only retired by an explicit newer note).
# Moving-target governance notes get a short TTL and are re-verified by the
# knowledge-assembler aux pass when the counter runs out. Unknown types default to
# ``None`` (conservative: never auto-expire something we don't recognise).
KB_TTL_BY_NOTE_TYPE: Dict[str, Optional[int]] = {
    # moving-target — expire + re-verify
    "state": 2,
    "goal": 3,
    "plan": 3,
    "question": 3,
    # durable — no TTL
    "decision": None,
    "learning": None,
    "code": None,
    "retrospective": None,
    "source": None,
}
KB_TTL_DEFAULT: Optional[int] = None


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


@dataclass
class KbWatermark:
    """The as-of git commit a KB's index was built from (slice 3, §5.1).

    One row per KB in ``kb_index_watermark``. The incremental reindexer (PR3)
    diffs the repo HEAD against ``indexed_commit`` and re-embeds only the changed
    blobs; ``pipeline_version`` (model id + dims + chunker version) invalidates the
    whole KB when it changes, forcing a full rebuild.
    """

    kb_id: Optional[uuid.UUID] = None
    repo_name: Optional[str] = None
    branch: Optional[str] = None
    indexed_commit: Optional[str] = None
    pipeline_version: Optional[str] = None
    updated_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "KbWatermark":
        """Create a KbWatermark from a database row dict."""
        return cls(
            kb_id=row.get("kb_id"),
            repo_name=row.get("repo_name"),
            branch=row.get("branch"),
            indexed_commit=row.get("indexed_commit"),
            pipeline_version=row.get("pipeline_version"),
            updated_at=row.get("updated_at"),
        )


@dataclass
class KnowledgeChunk:
    """A single heading-aware chunk of a note (slice 3, §5.1).

    In the chunk-granular model the dense vector lives on chunk rows
    (``knowledge_chunks``), not the note row. ``heading_path`` is the breadcrumb
    (``"Design > Storage > Chunking"``) prepended to the embed text; ``chunk_ix``
    is the 0-based position within the note. The raw ``embedding`` is not carried
    on the dataclass (mirrors KnowledgeRecord — vectors stay in the DB).
    """

    id: Optional[uuid.UUID] = None
    note_row: Optional[uuid.UUID] = None
    kb_id: Optional[uuid.UUID] = None
    chunk_ix: int = 0
    heading_path: Optional[str] = None
    content: str = ""
    embedding_version: Optional[str] = None
    created_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "KnowledgeChunk":
        """Create a KnowledgeChunk from a database row dict."""
        return cls(
            id=row.get("id"),
            note_row=row.get("note_row"),
            kb_id=row.get("kb_id"),
            chunk_ix=row.get("chunk_ix", 0),
            heading_path=row.get("heading_path"),
            content=row.get("content", ""),
            embedding_version=row.get("embedding_version"),
            created_at=row.get("created_at"),
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
    def _ttl_for_note_type(note_type: str) -> Optional[int]:
        """Initial TTL (loop cycles) for a new note of this type.

        ``None`` = durable (no countdown). See KB_TTL_BY_NOTE_TYPE.
        """
        return KB_TTL_BY_NOTE_TYPE.get(
            (note_type or "").strip().lower(), KB_TTL_DEFAULT
        )

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
                project_id,
                note_id,
                title,
                note_type,
                status,
                confidence,
                tags_list,
                keywords_list,
                job_id,
                phase,
                retrieval_list,
                modified_at,
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
        search_text = " ".join(
            [
                title,
                content,
                " ".join(tags_list),
                " ".join(keywords_list),
                " ".join(retrieval_list),
            ]
        )

        # Initial TTL (loop cycles) by note_type — set only on INSERT. The
        # ON CONFLICT branch below deliberately omits remaining_cycles so an
        # existing note's countdown is preserved across content edits, and a
        # status-only change (kb_update → superseded) takes the metadata-only
        # path above which never touches it. See kb_convergence design doc.
        ttl_value = self._ttl_for_note_type(note_type)

        row_id = await self.db.fetchval(
            """
            INSERT INTO knowledge_index (
                note_id, project_id, title, note_type, status, confidence,
                tags, keywords, job_id, phase, content, retrieval_messages,
                embedding, search_doc, created_at, modified_at, indexed_at,
                content_hash, remaining_cycles
            ) VALUES (
                $1, $2, $3, $4, $5, $6,
                $7, $8, $9, $10, $11, $12,
                $13, to_tsvector('english', $14), $15, $16, NOW(),
                $17, $18
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
            note_id,
            project_id,
            title,
            note_type,
            status,
            confidence,
            tags_list,
            keywords_list,
            job_id,
            phase,
            content,
            retrieval_list,
            embedding_vec,
            search_text,
            created_at,
            modified_at,
            new_hash,
            ttl_value,
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
    # Chunk-granular index — OKF files-canonical KB slice 3 (§5.1)
    #
    # Inert persistence primitives: the git-tree reindexer (PR3) writes these and
    # the retrieval cutover (PR4) reads them. Nothing in the running system calls
    # them yet — the schema (0008_kb_index_chunking.sql) and this surface land
    # together so PR3/PR4 are pure query code.
    # =========================================================================

    async def get_watermark(self, kb_id: uuid.UUID) -> Optional[KbWatermark]:
        """The index watermark for a KB, or ``None`` if never indexed.

        PR3 reads this to learn the commit the index was built as-of, then diffs
        the repo HEAD against it.
        """
        row = await self.db.fetchrow(
            "SELECT * FROM kb_index_watermark WHERE kb_id = $1",
            kb_id,
        )
        return KbWatermark.from_row(dict(row)) if row else None

    async def upsert_watermark(
        self,
        kb_id: uuid.UUID,
        repo_name: Optional[str],
        branch: Optional[str],
        indexed_commit: Optional[str],
        pipeline_version: Optional[str],
    ) -> None:
        """Record (or advance) the as-of commit for a KB's index.

        PR3 calls this once, at the END of a reindex — the watermark advances only
        after every changed row is written, so an interrupted run leaves the old
        commit and the next run re-diffs from there (per-row ``blob_sha`` skips the
        already-current rows).
        """
        await self.db.execute(
            """
            INSERT INTO kb_index_watermark
                (kb_id, repo_name, branch, indexed_commit, pipeline_version, updated_at)
            VALUES ($1, $2, $3, $4, $5, NOW())
            ON CONFLICT (kb_id) DO UPDATE
               SET repo_name = $2,
                   branch = $3,
                   indexed_commit = $4,
                   pipeline_version = $5,
                   updated_at = NOW()
            """,
            kb_id,
            repo_name,
            branch,
            indexed_commit,
            pipeline_version,
        )

    async def get_indexed_blob_shas(self, kb_id: uuid.UUID) -> Dict[str, str]:
        """``{path: blob_sha}`` for every indexed note in a KB.

        The tree-diff reindexer (PR3) compares this against the git tree's
        ``{path: blob_sha}`` map to decide which notes changed, were added, or were
        removed — the per-row self-heal that makes interrupted re-runs cheap.
        """
        rows = await self.db.fetch(
            """
            SELECT path, blob_sha FROM knowledge_index
            WHERE kb_id = $1 AND path IS NOT NULL
            """,
            kb_id,
        )
        return {r["path"]: r["blob_sha"] for r in rows}

    async def adopt_legacy_row(
        self, kb_id: uuid.UUID, note_id: str, path: str
    ) -> Optional[uuid.UUID]:
        """Claim a pre-slice-3 row for the files-canonical index.

        Legacy rows (written by the DB-first ``upsert_note`` path) carry
        ``(project_id, note_id)`` but no ``path``. A reindex INSERT for the same
        slug would violate ``uq_knowledge_project_note`` — an arbiter
        ``ON CONFLICT (kb_id, path)`` can't absorb a conflict on a *different*
        unique constraint. So the reindexer first adopts: set ``kb_id`` + ``path``
        on the pathless row (project-scoped KB: ``project_id`` doubles as the kb
        id), after which :meth:`upsert_kb_note` hits the ``(kb_id, path)`` arbiter
        normally. ``path IS NULL`` guards against stealing an already-migrated
        row. Returns the adopted row id, or ``None`` when there is no legacy row.
        """
        return await self.db.fetchval(
            """
            UPDATE knowledge_index
            SET kb_id = $1, path = $3
            WHERE project_id = $1 AND note_id = $2 AND path IS NULL
            RETURNING id
            """,
            kb_id,
            note_id,
            path,
        )

    async def upsert_kb_note(
        self,
        kb_id: uuid.UUID,
        note_id: str,
        path: str,
        title: str,
        note_type: str,
        content: str,
        blob_sha: Optional[str],
        embedding_version: Optional[str],
        *,
        status: str = "active",
        confidence: Optional[str] = None,
        tags: Optional[List[str]] = None,
        keywords: Optional[List[str]] = None,
        retrieval_messages: Optional[List[str]] = None,
        superseded_by: Optional[str] = None,
        invalidated_at: Optional[datetime] = None,
        job_id: Optional[uuid.UUID] = None,
        phase: Optional[int] = None,
        created_at: Optional[datetime] = None,
        modified_at: Optional[datetime] = None,
    ) -> uuid.UUID:
        """Upsert a note-level row keyed by ``(kb_id, path)``.

        The metadata half of a reindex write — the dense vectors live on
        ``knowledge_chunks`` (see :meth:`replace_note_chunks`), so this INSERT never
        touches the ``embedding`` column. The reindexer passes ``blob_sha`` /
        ``embedding_version`` as None and stamps them via
        :meth:`stamp_note_indexed` only after the chunk write succeeds; the row
        also carries ``project_id = kb_id`` so legacy project-scoped queries keep
        working through the transition. Returns the row id (for the chunk FK).
        """
        content_hash = self._content_hash(content)
        return await self.db.fetchval(
            """
            INSERT INTO knowledge_index
                (kb_id, project_id, note_id, path, title, note_type, content,
                 status, confidence, tags, keywords, retrieval_messages,
                 blob_sha, embedding_version, superseded_by, invalidated_at,
                 job_id, phase, content_hash, created_at, modified_at, indexed_at)
            VALUES
                ($1, $1, $2, $3, $4, $5, $6,
                 $7, $8, $9, $10, $11,
                 $12, $13, $14, $15,
                 $16, $17, $18, $19, $20, NOW())
            ON CONFLICT (kb_id, path) WHERE kb_id IS NOT NULL AND path IS NOT NULL
            DO UPDATE SET
                note_id = $2, title = $4, note_type = $5, content = $6,
                status = $7, confidence = $8, tags = $9, keywords = $10,
                retrieval_messages = $11, blob_sha = $12, embedding_version = $13,
                superseded_by = $14, invalidated_at = $15, job_id = $16,
                phase = $17, content_hash = $18, modified_at = $20, indexed_at = NOW()
            RETURNING id
            """,
            kb_id,
            note_id,
            path,
            title,
            note_type,
            content,
            status,
            confidence,
            tags or [],
            keywords or [],
            retrieval_messages or [],
            blob_sha,
            embedding_version,
            superseded_by,
            invalidated_at,
            job_id,
            phase,
            content_hash,
            created_at,
            modified_at,
        )

    async def stamp_note_indexed(
        self,
        note_row: uuid.UUID,
        blob_sha: str,
        embedding_version: str,
    ) -> None:
        """Mark a note's chunks as durably written (PR3.1).

        ``blob_sha``/``embedding_version`` on the note row mean "the chunk set for
        this git blob is fully persisted" — so the reindexer upserts the note
        UNSTAMPED, writes chunks, then stamps. A chunk-write failure leaves
        ``blob_sha`` NULL and the next tree-diff retries the note (a stamped-but-
        chunkless note would read as up-to-date forever — the live 2026-07-05
        zero-chunks gap).
        """
        await self.db.execute(
            """
            UPDATE knowledge_index
            SET blob_sha = $2, embedding_version = $3
            WHERE id = $1
            """,
            note_row,
            blob_sha,
            embedding_version,
        )

    async def replace_note_chunks(
        self,
        note_row: uuid.UUID,
        kb_id: uuid.UUID,
        chunks: List[Dict[str, Any]],
        embedding_version: str,
    ) -> int:
        """Replace a note's chunk rows wholesale — delete all, insert the new set.

        Called by the reindexer right after :meth:`upsert_kb_note`; the delete keeps
        chunk_ix contiguous when a note shrinks. Each chunk is a dict with
        ``chunk_ix``, ``content``, ``embedding`` (and optional ``heading_path``);
        ``search_doc`` is computed at write time so the sparse arm needs no trigger.
        Returns the number of chunks written.
        """
        await self.db.execute(
            "DELETE FROM knowledge_chunks WHERE note_row = $1",
            note_row,
        )
        for ch in chunks:
            await self.db.execute(
                """
                INSERT INTO knowledge_chunks
                    (note_row, kb_id, chunk_ix, heading_path, content,
                     embedding, search_doc, embedding_version)
                VALUES ($1, $2, $3, $4, $5, $6, to_tsvector('english', $5), $7)
                """,
                note_row,
                kb_id,
                ch["chunk_ix"],
                ch.get("heading_path"),
                ch["content"],
                self._prepare_embedding(ch["embedding"]),
                embedding_version,
            )
        return len(chunks)

    async def delete_kb_note(self, kb_id: uuid.UUID, path: str) -> bool:
        """Remove a note by ``(kb_id, path)``; its chunks cascade away.

        The reindexer calls this for notes that vanished from the git tree. The
        ``ON DELETE CASCADE`` on ``knowledge_chunks.note_row`` reaps the chunk rows.
        Returns True if a row was deleted.
        """
        result = await self.db.fetchval(
            """
            DELETE FROM knowledge_index
            WHERE kb_id = $1 AND path = $2
            RETURNING id
            """,
            kb_id,
            path,
        )
        return result is not None

    async def replace_note_links(
        self,
        source_note_row: uuid.UUID,
        kb_id: uuid.UUID,
        source_id: str,
        targets: List[str],
        rel_type: str = "references",
    ) -> int:
        """Replace a note's outbound link edges wholesale (slice-3 PR4c).

        The files-canonical successor to the Neo4j relationship edges: the
        reindexer extracts body markdown link targets and rewrites this note's
        edge set (delete all + re-insert, mirroring :meth:`replace_note_chunks`),
        so the 1-hop ``kb_related`` degrade has a link table to query when Neo4j
        is absent. Targets are stored as slugs and resolved to note rows at read
        time — a link to a not-yet-indexed note is kept, and dead links simply
        fall out of the read-time join. Repeated targets collapse to one edge.
        Returns the number of edges written.
        """
        await self.db.execute(
            "DELETE FROM knowledge_links WHERE source_note_row = $1",
            source_note_row,
        )
        seen: set = set()
        written = 0
        for target in targets:
            if target in seen:
                continue
            seen.add(target)
            await self.db.execute(
                """
                INSERT INTO knowledge_links
                    (source_note_row, kb_id, source_id, target_id, rel_type)
                VALUES ($1, $2, $3, $4, $5)
                """,
                source_note_row,
                kb_id,
                source_id,
                target,
                rel_type,
            )
            written += 1
        return written

    async def get_related_notes(
        self,
        kb_id: uuid.UUID,
        note_id: str,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """1-hop link neighbours of a note (the kg-less ``kb_related`` backend).

        Neighbours are notes ``note_id`` links to (outbound) or that link to it
        (inbound), resolved against ``knowledge_index`` for title/type/status and
        filtered to ``status = 'active'`` — dead links (targets with no active
        note) drop out of the join. Returns the same shape as the Neo4j
        ``get_related`` (``id``/``title``/``type``/``status``/``distance``/
        ``rel_types``) so the tool formats both worlds identically; every edge is
        a generic body-markdown ``references`` link, hence ``distance = 1`` and a
        constant ``rel_types``.
        """
        rows = await self.db.fetch(
            """
            SELECT DISTINCT ki.note_id AS id, ki.title AS title,
                   ki.note_type AS type, ki.status AS status
            FROM (
                SELECT target_id AS neighbour FROM knowledge_links
                WHERE kb_id = $1 AND source_id = $2
                UNION
                SELECT source_id AS neighbour FROM knowledge_links
                WHERE kb_id = $1 AND target_id = $2
            ) nbr
            JOIN knowledge_index ki
              ON ki.kb_id = $1 AND ki.note_id = nbr.neighbour
            WHERE ki.status = 'active'
            ORDER BY ki.title
            LIMIT $3
            """,
            kb_id,
            note_id,
            limit,
        )
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "type": r["type"],
                "status": r["status"],
                "distance": 1,
                "rel_types": ["references"],
            }
            for r in rows
        ]

    async def get_note_by_slug(
        self, kb_id: uuid.UUID, note_id: str
    ) -> Optional[Dict[str, Any]]:
        """A full note read from the index (the kg-less ``kb_read`` backend).

        Returns the note in the same dict shape ``KnowledgeGraphDB.read_note``
        produces (``id``/``type`` rather than ``note_id``/``note_type``) so the
        tool formats both worlds identically. Status-agnostic — a direct read
        must surface superseded/archived notes too, unlike search. Neo4j-only
        fields (``relationships``) are absent; 1-hop links come from
        :meth:`get_related_notes`. Returns None if the slug isn't in the KB.
        """
        row = await self.db.fetchrow(
            """
            SELECT note_id, title, note_type, status, content, confidence,
                   tags, keywords, job_id, phase, created_at, modified_at
            FROM knowledge_index
            WHERE kb_id = $1 AND note_id = $2
            LIMIT 1
            """,
            kb_id,
            note_id,
        )
        if row is None:
            return None
        r = dict(row)
        return {
            "id": r.get("note_id"),
            "title": r.get("title", ""),
            "type": r.get("note_type", ""),
            "status": r.get("status", ""),
            "content": r.get("content", ""),
            "confidence": r.get("confidence"),
            "tags": r.get("tags") or [],
            "keywords": r.get("keywords") or [],
            "job_id": r.get("job_id"),
            "phase": r.get("phase"),
            "created": r.get("created_at"),
            "modified": r.get("modified_at"),
        }

    async def list_notes(
        self,
        kb_id: uuid.UUID,
        note_type: Optional[str] = None,
        tag: Optional[str] = None,
        status: Optional[str] = None,
        job_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """List note summaries in a KB (the kg-less ``kb_list`` backend).

        Same filter surface and summary-dict shape as
        ``KnowledgeGraphDB.list_notes`` (``id``/``type``); ``tag`` matches against
        the ``tags`` array column. No status filter is applied unless requested,
        so an unfiltered list spans every status like the Neo4j version.
        """
        conditions = ["kb_id = $1"]
        params: List[Any] = [kb_id]
        if note_type:
            params.append(note_type)
            conditions.append(f"note_type = ${len(params)}")
        if status:
            params.append(status)
            conditions.append(f"status = ${len(params)}")
        if job_id:
            params.append(job_id)
            conditions.append(f"job_id = ${len(params)}")
        if tag:
            params.append(tag)
            conditions.append(f"${len(params)} = ANY(tags)")
        params.append(limit)
        where = " AND ".join(conditions)
        rows = await self.db.fetch(
            f"""
            SELECT note_id, title, note_type, status, confidence
            FROM knowledge_index
            WHERE {where}
            ORDER BY modified_at DESC NULLS LAST
            LIMIT ${len(params)}
            """,
            *params,
        )
        return [
            {
                "id": r["note_id"],
                "title": r["title"],
                "type": r["note_type"],
                "status": r["status"],
                "confidence": r["confidence"],
            }
            for r in rows
        ]

    # =========================================================================
    # TTL lifecycle — KB convergence
    # (docs/features/kb_convergence_ttl_reverification.md)
    # =========================================================================

    async def decrement_ttl(self, project_id: uuid.UUID) -> int:
        """Decrement the cycle TTL of every active, TTL-bearing note in a project.

        Called once per loop cycle (at the developer→scholar wrap). Durable notes
        (``remaining_cycles IS NULL``) are untouched; only moving-target notes
        count down. A note reaching ``<= 0`` enters the stale queue
        (:meth:`get_stale_notes`) for re-verification. Returns rows decremented.

        NOTE: the orchestrator runs the equivalent UPDATE inline against the
        vector DB at the cycle boundary (it can't safely import this module). This
        method is the canonical agent-side / test form — keep the two in sync.
        """
        rows = await self.db.fetch(
            """
            UPDATE knowledge_index
            SET remaining_cycles = remaining_cycles - 1
            WHERE project_id = $1
              AND remaining_cycles IS NOT NULL
              AND status = 'active'
            RETURNING id
            """,
            project_id,
        )
        return len(rows)

    async def get_stale_notes(
        self, project_id: uuid.UUID, limit: int = 50
    ) -> List[KnowledgeRecord]:
        """Active notes whose TTL has run out (``remaining_cycles <= 0``).

        The stale queue the knowledge-assembler re-verifies. Oldest-modified
        first, so the most-likely-stale are handled first under a limit.
        """
        rows = await self.db.fetch(
            """
            SELECT * FROM knowledge_index
            WHERE project_id = $1
              AND remaining_cycles <= 0
              AND status = 'active'
            ORDER BY modified_at ASC NULLS FIRST
            LIMIT $2
            """,
            project_id,
            limit,
        )
        return [KnowledgeRecord.from_row(dict(r)) for r in rows]

    async def refresh_ttl(
        self,
        project_id: uuid.UUID,
        notes: List[KnowledgeRecord],
        current_cycle: Optional[int] = None,
    ) -> int:
        """Reset the TTL of notes that survived re-verification.

        Called by the assembler runner AFTER the agent pass. Only notes still
        ``active`` are refreshed (the WHERE clause skips any the agent superseded
        or archived), each back to its note_type's initial TTL. Durable notes are
        ignored. Returns the number of notes refreshed.
        """
        refreshed = 0
        for note in notes:
            ttl = self._ttl_for_note_type(note.note_type)
            if ttl is None:
                continue
            rows = await self.db.fetch(
                """
                UPDATE knowledge_index
                SET remaining_cycles = $1,
                    last_verified_cycle = $2
                WHERE project_id = $3 AND note_id = $4 AND status = 'active'
                RETURNING id
                """,
                ttl,
                current_cycle,
                project_id,
                note.note_id,
            )
            refreshed += len(rows)
        return refreshed

    # =========================================================================
    # Search
    # =========================================================================

    async def hybrid_search(
        self,
        project_id: Optional[uuid.UUID] = None,
        query: str = "",
        match_count: int = 10,
        dense_weight: float = 0.6,
        sparse_weight: float = 0.3,
        recency_weight: float = 0.1,
        project_ids: Optional[List[uuid.UUID]] = None,
    ) -> List[KnowledgeRecord]:
        """Execute hybrid search (RRF over dense + sparse + recency).

        Args:
            project_id: Single project UUID (backward compat)
            query: Search query text
            match_count: Max results
            dense_weight: Weight for vector similarity
            sparse_weight: Weight for keyword match
            recency_weight: Weight for recency
            project_ids: List of project UUIDs for multi-project search

        Returns:
            List of KnowledgeRecord objects ranked by RRF score
        """
        # Normalize: project_ids takes priority, fall back to project_id
        effective_ids = project_ids or ([project_id] if project_id else [])
        if not effective_ids:
            return []

        query_embedding = await self.embedding_service.embed(query)

        if len(effective_ids) > 1:
            func_name = "knowledge_multi_project_hybrid_search"
            scope_val = effective_ids
        else:
            func_name = "knowledge_hybrid_search"
            scope_val = effective_ids[0]

        rows = await self.db.fetch(
            f"""
            SELECT * FROM {func_name}(
                $1, $2, $3, $4, $5, $6, $7
            )
            """,
            query,
            query_embedding,
            scope_val,
            match_count,
            dense_weight,
            sparse_weight,
            recency_weight,
        )

        return [KnowledgeRecord.from_row(dict(row)) for row in rows]

    async def find_similar_many(
        self,
        project_id: uuid.UUID,
        embedding: Union[List[float], str],
        k: int = 5,
        min_similarity: float = 0.6,
    ) -> List[KnowledgeRecord]:
        """Return the top-``k`` active neighbours above ``min_similarity``.

        The KB analog of ``RecallStore.find_similar_many`` — the neighbour fetch
        that feeds the ingestion verdict (slice 2 PR2). Only ``status = 'active'``
        rows are candidates: a verdict never compares a new note against an
        already superseded/archived one. Each returned record carries a transient
        ``.similarity`` (cosine, closest first) for the verdict prompt; an empty
        list is the cost-guard signal (no neighbour above the floor → straight
        ADD, no LLM call).
        """
        embedding_vec = self._prepare_embedding(embedding)
        rows = await self.db.fetch(
            """
            SELECT *, 1 - (embedding <=> $1) AS similarity
            FROM knowledge_index
            WHERE project_id = $2
              AND embedding IS NOT NULL
              AND status = 'active'
              AND 1 - (embedding <=> $1) >= $3
            ORDER BY similarity DESC
            LIMIT $4
            """,
            embedding_vec,
            project_id,
            min_similarity,
            k,
        )
        records: List[KnowledgeRecord] = []
        for row in rows:
            row_dict = dict(row)
            rec = KnowledgeRecord.from_row(row_dict)
            rec.similarity = row_dict.get("similarity")
            records.append(rec)
        return records

    async def find_near_duplicate_pairs(
        self,
        project_id: uuid.UUID,
        min_similarity: float = 0.9,
        limit: int = 50,
    ) -> List[Tuple[str, str, float]]:
        """All active note pairs whose embeddings are near-duplicates.

        One self-join over the index (each unordered pair once, via
        ``a.note_id < b.note_id``), feeding ``kb_lint``'s near-duplicate rule —
        detection for the bare-writer twins the verdict gate never adjudicates.
        Most-similar pairs first. The 0.9 default floor is deliberately far
        above the verdict fetch floor (0.6): lint flags only what a gardener
        should merge, not everything topically related.
        """
        rows = await self.db.fetch(
            """
            SELECT a.note_id AS note_a, b.note_id AS note_b,
                   1 - (a.embedding <=> b.embedding) AS similarity
            FROM knowledge_index a
            JOIN knowledge_index b
              ON b.project_id = a.project_id
             AND a.note_id < b.note_id
            WHERE a.project_id = $1
              AND a.status = 'active'
              AND b.status = 'active'
              AND a.embedding IS NOT NULL
              AND b.embedding IS NOT NULL
              AND 1 - (a.embedding <=> b.embedding) >= $2
            ORDER BY similarity DESC
            LIMIT $3
            """,
            project_id,
            min_similarity,
            limit,
        )
        return [
            (str(r["note_a"]), str(r["note_b"]), float(r["similarity"])) for r in rows
        ]

    async def search_chunks(
        self,
        kb_ids: List[uuid.UUID],
        query: str = "",
        embedding_version: Optional[str] = None,
        match_count: int = 15,
        over_fetch: int = 50,
        dense_weight: float = 0.6,
        sparse_weight: float = 0.3,
        recency_weight: float = 0.1,
        rrf_k: int = 60,
    ) -> List[KnowledgeRecord]:
        """Hybrid retrieval over the chunk index (slice-3 PR4, §5.1).

        The chunk-granular successor to :meth:`hybrid_search`. After the PR3
        reindexer the dense vector lives on ``knowledge_chunks`` (the note row's
        ``embedding`` is NULL for reindexed notes), so fusion runs over chunk
        rows — dense (chunk embedding) + sparse (chunk ``search_doc``) + recency
        (note ``modified_at``) — and the best-scoring chunk collapses back to its
        note. Returns note-level ``KnowledgeRecord``s so the ``kb_search`` tool
        signature is unchanged; the ``status = 'active'`` filter is preserved
        exactly (stricter than "not superseded").

        The SQL function over-fetches ``over_fetch`` fused note candidates; this
        method then runs the reranker slot (a no-op passthrough in v1 — a hosted
        or local reranker wires in here as a precision mode) and truncates to
        ``match_count``. ``embedding_version`` filters mixed-model vectors out so
        a model migration can't silently drift the index; ``None`` disables the
        filter (single-model deployments). An empty ``kb_ids`` is the cost guard.
        """
        if not kb_ids:
            return []

        query_embedding = await self.embedding_service.embed(query)

        rows = await self.db.fetch(
            """
            SELECT * FROM knowledge_chunk_hybrid_search(
                $1, $2, $3, $4, $5, $6, $7, $8, $9
            )
            """,
            query,
            query_embedding,
            list(kb_ids),
            embedding_version,
            over_fetch,
            dense_weight,
            sparse_weight,
            recency_weight,
            rrf_k,
        )

        records = [KnowledgeRecord.from_row(dict(row)) for row in rows]
        return self._rerank_chunks(records)[:match_count]

    @staticmethod
    def _rerank_chunks(records: List[KnowledgeRecord]) -> List[KnowledgeRecord]:
        """Reranker slot between RRF fusion and return (§5.1).

        v1 is a no-op passthrough that preserves the fusion order — the single
        biggest post-hybrid quality lever, but agents can grep-and-read after
        search, so a hosted/local cross-encoder reranker is a later ``rerank:
        true`` precision mode that slots in here without touching callers.
        """
        return records

    async def get_summary(
        self,
        project_id: Optional[uuid.UUID] = None,
        project_ids: Optional[List[uuid.UUID]] = None,
    ) -> Dict[str, Any]:
        """Get knowledge base summary for context injection.

        Args:
            project_id: Single project UUID (backward compat)
            project_ids: List of project UUIDs for multi-project summary

        Returns:
            Dict with note counts by type, total count, recent notes
        """
        effective_ids = project_ids or ([project_id] if project_id else [])
        if not effective_ids:
            return {"total": 0}

        if len(effective_ids) > 1:
            scope_clause = "project_id = ANY($1)"
            scope_val = effective_ids
        else:
            scope_clause = "project_id = $1"
            scope_val = effective_ids[0]

        row = await self.db.fetchrow(
            f"""
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
            WHERE {scope_clause}
            """,
            scope_val,
        )

        summary = dict(row) if row else {"total": 0}

        # Get 5 most recent notes
        recent = await self.db.fetch(
            f"""
            SELECT note_id, title, note_type, status, modified_at
            FROM knowledge_index
            WHERE {scope_clause} AND status = 'active'
            ORDER BY modified_at DESC
            LIMIT 5
            """,
            scope_val,
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
                if hasattr(created, "to_native"):
                    created = created.to_native()
                if hasattr(modified, "to_native"):
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
        model: Optional[str] = None,
    ) -> str:
        """Assemble formatted knowledge block for context injection.

        Args:
            notes: List of KnowledgeRecord objects
            model: Model id used to resolve family-specific block headers
                / footer. Falls through to the default family if None.

        Returns:
            Formatted knowledge block string
        """
        if not notes:
            return ""

        from src.services.guardrails import format_nudge

        lines = [format_nudge("knowledge_block_header", model=model), ""]
        for i, note in enumerate(notes, 1):
            lines.append(cls.format_note(note, i))
            lines.append("")

        tokens_est = sum(len(n.content) // 4 for n in notes)
        lines.append(
            format_nudge(
                "knowledge_block_footer",
                model=model,
                count=len(notes),
                tokens=f"{tokens_est:,}",
            )
        )
        return "\n".join(lines)


__all__ = ["KnowledgeStore", "KnowledgeRecord"]
