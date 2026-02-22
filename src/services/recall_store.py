"""RecallStore — Memory Light Phase 1: storage + retrieval.

Provides hybrid search (dense vector + sparse keyword + recency) over
agent memories stored in PostgreSQL with pgvector.

See docs/features/memory_light.md for full architecture.

Usage:
    ```python
    from src.services.recall_store import RecallStore
    from src.services.embedding_service import get_embedding_service

    store = RecallStore(
        db=postgres_db,
        embedding_service=get_embedding_service(),
        job_id=job_id,
        config=agent_config.memory,
    )

    # Store a memory
    mem_id = await store.store(
        content="The users table uses soft deletes via deleted_at.",
        summary="Users table has soft deletes",
        keywords=["users", "soft_delete", "deleted_at"],
        importance=0.8,
    )

    # Retrieve relevant memories
    memories = await store.retrieve(
        context_text="How should I query the users table?",
        budget_tokens=5000,
    )
    ```
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# English stopwords — small hardcoded set for keyword extraction
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "was", "are", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "shall", "can", "this", "that",
    "these", "those", "it", "its", "i", "me", "my", "we", "our", "you",
    "your", "he", "she", "his", "her", "they", "them", "their", "not",
    "no", "if", "then", "else", "when", "up", "out", "so", "as", "all",
    "each", "every", "both", "few", "more", "most", "other", "some", "such",
    "into", "over", "after", "before", "between", "under", "about", "than",
})


def extract_keywords(text: str, max_keywords: int = 8) -> List[str]:
    """Extract keywords from text by filtering stopwords.

    Splits on whitespace/punctuation, removes stopwords, lowercases,
    deduplicates, and returns the first ``max_keywords`` tokens.

    Args:
        text: Input text
        max_keywords: Maximum number of keywords to return

    Returns:
        List of keyword strings
    """
    import re
    tokens = re.split(r'[\s\W]+', text.lower())
    seen: set = set()
    result: List[str] = []
    for token in tokens:
        if len(token) < 2 or token in _STOPWORDS or token in seen:
            continue
        seen.add(token)
        result.append(token)
        if len(result) >= max_keywords:
            break
    return result


@dataclass
class MemoryRecord:
    """A single memory entry."""

    id: Optional[uuid.UUID] = None
    job_id: Optional[uuid.UUID] = None
    agent_id: Optional[str] = None
    content: str = ""
    summary: Optional[str] = None
    memory_type: str = "factual"
    source: str = "observer"
    keywords: List[str] = field(default_factory=list)
    embedding: Optional[List[float]] = None
    importance: float = 0.5
    source_turn_start: Optional[int] = None
    source_turn_end: Optional[int] = None
    source_phase: Optional[int] = None
    token_count: int = 0
    access_count: int = 0
    created_at: Optional[datetime] = None
    last_accessed: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "MemoryRecord":
        """Create a MemoryRecord from a database row dict."""
        return cls(
            id=row.get("id"),
            job_id=row.get("job_id"),
            agent_id=row.get("agent_id"),
            content=row.get("content", ""),
            summary=row.get("summary"),
            memory_type=row.get("memory_type", "factual"),
            source=row.get("source", "observer"),
            keywords=row.get("keywords") or [],
            importance=row.get("importance", 0.5),
            source_turn_start=row.get("source_turn_start"),
            source_turn_end=row.get("source_turn_end"),
            source_phase=row.get("source_phase"),
            token_count=row.get("token_count", 0),
            access_count=row.get("access_count", 0),
            created_at=row.get("created_at"),
            last_accessed=row.get("last_accessed"),
        )


class RecallStore:
    """Memory storage and retrieval with hybrid search.

    Manages agent memories in PostgreSQL with three retrieval channels:
    1. Dense vector search (cosine similarity via pgvector)
    2. Sparse keyword search (PostgreSQL tsvector/tsquery)
    3. Recency (most recently created memories)

    Results are fused via Reciprocal Rank Fusion (RRF) using the
    `memory_hybrid_search()` SQL function.
    """

    def __init__(
        self,
        db,
        embedding_service,
        job_id: uuid.UUID,
        config=None,
        agent_id: Optional[str] = None,
    ):
        """Initialize RecallStore.

        Args:
            db: PostgresDB instance (agent-side or orchestrator-side)
            embedding_service: EmbeddingService for generating vectors
            job_id: Job UUID (memories are scoped per job)
            config: MemoryConfig dataclass (optional, uses defaults if None)
            agent_id: Optional agent identifier for cross-job memory (Phase 5)
        """
        self.db = db
        self.embedding_service = embedding_service
        self.job_id = job_id
        self.agent_id = agent_id

        # Config defaults (matches MemoryConfig dataclass)
        self.dedup_threshold = 0.92
        self.importance_threshold = 0.3
        self.dense_results = 5
        self.sparse_results = 5
        self.recent_results = 3
        self.budget_tokens = 5000
        self.max_memories_per_injection = 10

        if config is not None:
            self.dedup_threshold = getattr(config, "dedup_threshold", 0.92)
            self.importance_threshold = getattr(config, "importance_threshold", 0.3)
            self.dense_results = getattr(config, "dense_results", 5)
            self.sparse_results = getattr(config, "sparse_results", 5)
            self.recent_results = getattr(config, "recent_results", 3)
            self.budget_tokens = getattr(config, "budget_tokens", 5000)
            self.max_memories_per_injection = getattr(
                config, "max_memories_per_injection", 10
            )

    # =========================================================================
    # Storage
    # =========================================================================

    async def store(
        self,
        content: str,
        summary: Optional[str] = None,
        keywords: Optional[List[str]] = None,
        importance: float = 0.5,
        memory_type: str = "factual",
        source: str = "observer",
        source_turn_start: Optional[int] = None,
        source_turn_end: Optional[int] = None,
        source_phase: Optional[int] = None,
        token_count: Optional[int] = None,
    ) -> Optional[uuid.UUID]:
        """Store a memory with automatic embedding and dedup.

        If a semantically similar memory already exists (cosine > dedup_threshold),
        updates the existing memory's access_count and last_accessed instead.

        Args:
            content: Memory content text
            summary: One-line summary (optional)
            keywords: Keywords for sparse search
            importance: Importance score 0-1
            memory_type: factual, procedural, error_solution, vocabulary, relational
            source: observer, todo, compaction, phase_archive, tool_error
            source_turn_start: Start turn of source conversation
            source_turn_end: End turn of source conversation
            source_phase: Phase number when extracted
            token_count: Pre-counted tokens (estimated if None)

        Returns:
            UUID of stored/updated memory, or None if below importance threshold
        """
        if importance < self.importance_threshold:
            logger.debug(
                f"Memory below importance threshold ({importance} < "
                f"{self.importance_threshold}), skipping"
            )
            return None

        # Generate embedding
        embedding = await self.embedding_service.embed(content)

        # Check for duplicates
        existing = await self.find_similar(embedding, self.dedup_threshold)
        if existing:
            logger.debug(
                f"Dedup: updating existing memory {existing.id} "
                f"instead of creating new"
            )
            await self.db.execute(
                """
                UPDATE memories
                SET access_count = access_count + 1,
                    last_accessed = CURRENT_TIMESTAMP,
                    importance = GREATEST(importance, $1)
                WHERE id = $2
                """,
                importance,
                existing.id,
            )
            return existing.id

        # Estimate token count if not provided
        if token_count is None:
            # Rough estimate: ~4 chars per token
            token_count = len(content) // 4

        # Build tsvector for sparse search
        keywords_list = keywords or []
        keywords_text = " ".join(keywords_list) + " " + content

        mem_id = await self.db.fetchval(
            """
            INSERT INTO memories (
                job_id, agent_id, content, summary, memory_type, source,
                keywords, embedding, sparse_keywords,
                importance, source_turn_start, source_turn_end, source_phase,
                token_count
            ) VALUES (
                $1, $2, $3, $4, $5, $6,
                $7, $8, to_tsvector('english', $9),
                $10, $11, $12, $13,
                $14
            )
            RETURNING id
            """,
            self.job_id,
            self.agent_id,
            content,
            summary[:500] if summary else None,
            memory_type,
            source,
            keywords_list,
            embedding,
            keywords_text,
            importance,
            source_turn_start,
            source_turn_end,
            source_phase,
            token_count,
        )

        logger.debug(
            f"Stored memory {mem_id} (type={memory_type}, source={source}, "
            f"importance={importance})"
        )
        return mem_id

    # =========================================================================
    # Deduplication
    # =========================================================================

    async def find_similar(
        self,
        embedding: List[float],
        threshold: Optional[float] = None,
    ) -> Optional[MemoryRecord]:
        """Find a semantically similar existing memory.

        Args:
            embedding: Query embedding vector
            threshold: Cosine similarity threshold (default: self.dedup_threshold)

        Returns:
            Most similar MemoryRecord if above threshold, else None
        """
        threshold = threshold if threshold is not None else self.dedup_threshold

        row = await self.db.fetchrow(
            """
            SELECT *, 1 - (embedding <=> $1) AS similarity
            FROM memories
            WHERE job_id = $2
              AND embedding IS NOT NULL
              AND 1 - (embedding <=> $1) > $3
            ORDER BY similarity DESC
            LIMIT 1
            """,
            embedding,
            self.job_id,
            threshold,
        )

        if row:
            return MemoryRecord.from_row(dict(row))
        return None

    # =========================================================================
    # Retrieval
    # =========================================================================

    async def search_dense(
        self,
        embedding: List[float],
        limit: Optional[int] = None,
    ) -> List[MemoryRecord]:
        """Search memories by dense vector similarity.

        Args:
            embedding: Query embedding vector
            limit: Max results (default: self.dense_results)

        Returns:
            List of matching MemoryRecord objects
        """
        limit = limit or self.dense_results

        rows = await self.db.fetch(
            """
            SELECT *
            FROM memories
            WHERE job_id = $1 AND embedding IS NOT NULL
            ORDER BY embedding <=> $2
            LIMIT $3
            """,
            self.job_id,
            embedding,
            limit,
        )

        # Update access tracking
        if rows:
            ids = [row["id"] for row in rows]
            await self.db.execute(
                """
                UPDATE memories
                SET access_count = access_count + 1,
                    last_accessed = CURRENT_TIMESTAMP
                WHERE id = ANY($1)
                """,
                ids,
            )

        return [MemoryRecord.from_row(dict(row)) for row in rows]

    async def search_sparse(
        self,
        query_text: str,
        limit: Optional[int] = None,
    ) -> List[MemoryRecord]:
        """Search memories by keyword/full-text search.

        Args:
            query_text: Text query for tsquery matching
            limit: Max results (default: self.sparse_results)

        Returns:
            List of matching MemoryRecord objects
        """
        limit = limit or self.sparse_results

        rows = await self.db.fetch(
            """
            SELECT *,
                   ts_rank_cd(sparse_keywords, websearch_to_tsquery('english', $2)) AS rank
            FROM memories
            WHERE job_id = $1
              AND sparse_keywords @@ websearch_to_tsquery('english', $2)
            ORDER BY rank DESC
            LIMIT $3
            """,
            self.job_id,
            query_text,
            limit,
        )

        return [MemoryRecord.from_row(dict(row)) for row in rows]

    async def get_recent(
        self,
        limit: Optional[int] = None,
    ) -> List[MemoryRecord]:
        """Get most recently created memories.

        Args:
            limit: Max results (default: self.recent_results)

        Returns:
            List of MemoryRecord objects ordered by creation time
        """
        limit = limit or self.recent_results

        rows = await self.db.fetch(
            """
            SELECT *
            FROM memories
            WHERE job_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            self.job_id,
            limit,
        )

        return [MemoryRecord.from_row(dict(row)) for row in rows]

    async def hybrid_search(
        self,
        query_text: str,
        query_embedding: List[float],
        match_count: Optional[int] = None,
        dense_weight: float = 0.6,
        sparse_weight: float = 0.3,
        recency_weight: float = 0.1,
    ) -> List[MemoryRecord]:
        """Execute hybrid search using the SQL RRF function.

        Combines dense vector, sparse keyword, and recency channels
        via Reciprocal Rank Fusion.

        Args:
            query_text: Text query for sparse channel
            query_embedding: Embedding vector for dense channel
            match_count: Max results (default: max_memories_per_injection)
            dense_weight: Weight for dense vector channel
            sparse_weight: Weight for sparse keyword channel
            recency_weight: Weight for recency channel

        Returns:
            List of MemoryRecord objects ranked by RRF score
        """
        match_count = match_count or self.max_memories_per_injection

        rows = await self.db.fetch(
            """
            SELECT * FROM memory_hybrid_search(
                $1, $2, $3, $4, $5, $6, $7
            )
            """,
            query_text,
            query_embedding,
            self.job_id,
            match_count,
            dense_weight,
            sparse_weight,
            recency_weight,
        )

        # Update access tracking
        if rows:
            ids = [row["id"] for row in rows]
            await self.db.execute(
                """
                UPDATE memories
                SET access_count = access_count + 1,
                    last_accessed = CURRENT_TIMESTAMP
                WHERE id = ANY($1)
                """,
                ids,
            )

        return [MemoryRecord.from_row(dict(row)) for row in rows]

    async def retrieve(
        self,
        context_text: str,
        budget_tokens: Optional[int] = None,
    ) -> List[MemoryRecord]:
        """High-level retrieval: embed context, run hybrid search, fit to budget.

        This is the main entry point for memory retrieval. It:
        1. Embeds the context text
        2. Runs hybrid search (dense + sparse + recency via RRF)
        3. Trims results to fit within the token budget

        Args:
            context_text: Current context (e.g., current todo + recent messages)
            budget_tokens: Token budget for memory injection

        Returns:
            List of MemoryRecord objects that fit within budget
        """
        budget = budget_tokens or self.budget_tokens

        # Embed the context for dense search
        context_embedding = await self.embedding_service.embed(context_text)

        # Run hybrid search
        candidates = await self.hybrid_search(
            query_text=context_text,
            query_embedding=context_embedding,
        )

        # Trim to budget
        result = []
        tokens_used = 0
        for memory in candidates:
            if tokens_used + memory.token_count > budget:
                break
            result.append(memory)
            tokens_used += memory.token_count

        logger.debug(
            f"Retrieved {len(result)} memories ({tokens_used} tokens) "
            f"from {len(candidates)} candidates (budget={budget})"
        )
        return result

    # =========================================================================
    # Assembly
    # =========================================================================

    @staticmethod
    def format_memory(memory: MemoryRecord, index: int) -> str:
        """Format a single memory for injection.

        Args:
            memory: MemoryRecord to format
            index: Display index (1-based)

        Returns:
            Formatted memory string
        """
        meta_parts = []
        if memory.importance is not None:
            meta_parts.append(f"importance: {memory.importance:.1f}")
        if memory.source_phase is not None:
            meta_parts.append(f"phase {memory.source_phase}")
        if memory.memory_type and memory.memory_type != "factual":
            meta_parts.append(memory.memory_type)

        meta = f" ({', '.join(meta_parts)})" if meta_parts else ""
        return f"[{index}]{meta}\n{memory.content}"

    @classmethod
    def assemble_memory_block(
        cls,
        memories: List[MemoryRecord],
        budget_tokens: int = 5000,
    ) -> str:
        """Assemble formatted memory block for injection.

        Args:
            memories: List of MemoryRecord objects
            budget_tokens: Token budget (for display in footer)

        Returns:
            Formatted memory block string
        """
        if not memories:
            return ""

        lines = ["--- Relevant Memories ---", ""]
        tokens_used = sum(m.token_count for m in memories)

        for i, memory in enumerate(memories, 1):
            lines.append(cls.format_memory(memory, i))
            lines.append("")

        lines.append(
            f"--- End Memories ({len(memories)} items, ~{tokens_used:,} tokens) ---"
        )
        return "\n".join(lines)

    # =========================================================================
    # Stats
    # =========================================================================

    async def get_stats(self) -> Dict[str, Any]:
        """Get memory statistics for this job.

        Returns:
            Dict with counts by type, source, total tokens, etc.
        """
        row = await self.db.fetchrow(
            """
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(token_count), 0) AS total_tokens,
                COALESCE(SUM(access_count), 0) AS total_accesses,
                COUNT(*) FILTER (WHERE memory_type = 'factual') AS factual,
                COUNT(*) FILTER (WHERE memory_type = 'procedural') AS procedural,
                COUNT(*) FILTER (WHERE memory_type = 'error_solution') AS error_solution,
                COUNT(*) FILTER (WHERE memory_type = 'vocabulary') AS vocabulary,
                COUNT(*) FILTER (WHERE memory_type = 'relational') AS relational,
                COUNT(*) FILTER (WHERE source = 'observer') AS from_observer,
                COUNT(*) FILTER (WHERE source = 'todo') AS from_todo,
                COUNT(*) FILTER (WHERE source = 'compaction') AS from_compaction,
                COUNT(*) FILTER (WHERE source = 'phase_archive') AS from_phase_archive,
                COUNT(*) FILTER (WHERE source = 'tool_error') AS from_tool_error,
                AVG(importance) AS avg_importance
            FROM memories
            WHERE job_id = $1
            """,
            self.job_id,
        )

        if row:
            return dict(row)
        return {"total": 0, "total_tokens": 0}
