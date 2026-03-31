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
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "is",
        "was",
        "are",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "shall",
        "can",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "i",
        "me",
        "my",
        "we",
        "our",
        "you",
        "your",
        "he",
        "she",
        "his",
        "her",
        "they",
        "them",
        "their",
        "not",
        "no",
        "if",
        "then",
        "else",
        "when",
        "up",
        "out",
        "so",
        "as",
        "all",
        "each",
        "every",
        "both",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "into",
        "over",
        "after",
        "before",
        "between",
        "under",
        "about",
        "than",
    }
)


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

    tokens = re.split(r"[\s\W]+", text.lower())
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
    project_id: Optional[uuid.UUID] = None
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
    remaining_turns: Optional[int] = None
    created_at: Optional[datetime] = None
    last_accessed: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "MemoryRecord":
        """Create a MemoryRecord from a database row dict."""
        return cls(
            id=row.get("id"),
            job_id=row.get("job_id"),
            project_id=row.get("project_id"),
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
            remaining_turns=row.get("remaining_turns"),
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
        project_id: Optional[uuid.UUID] = None,
            project_ids: Optional[List[uuid.UUID]] = None,
        archiver=None,
    ):
        """Initialize RecallStore.

        Args:
            db: PostgresDB instance (agent-side or orchestrator-side)
            embedding_service: EmbeddingService for generating vectors
            job_id: Job UUID (memories are scoped per job)
            config: MemoryConfig dataclass (optional, uses defaults if None)
            agent_id: Optional agent identifier for cross-job memory (Phase 5)
            project_id: Optional project UUID for project-scoped memory sharing
            project_ids: Optional list of project UUIDs for multi-project sessions
            archiver: Optional LLMArchiver for audit logging
        """
        self.db = db
        self.embedding_service = embedding_service
        self.job_id = job_id
        self.agent_id = agent_id
        self.project_id = project_id
        self.project_ids = project_ids or ([project_id] if project_id else [])
        self._archiver = archiver
        self.project_scoped = (
            getattr(config, "project_scoped", False) if config else False
        )

        # Config defaults (matches MemoryConfig dataclass)
        self.dedup_threshold = 0.85
        self.importance_threshold = 0.3
        self.dense_results = 5
        self.sparse_results = 5
        self.recent_results = 3
        self.budget_tokens = 10000
        self.max_memories_per_injection = 150
        self.retrieval_importance_floor = 0.4
        self.default_ttl = 10

        if config is not None:
            self.dedup_threshold = getattr(config, "dedup_threshold", 0.85)
            self.importance_threshold = getattr(config, "importance_threshold", 0.3)
            self.dense_results = getattr(config, "dense_results", 5)
            self.sparse_results = getattr(config, "sparse_results", 5)
            self.recent_results = getattr(config, "recent_results", 3)
            self.budget_tokens = getattr(config, "budget_tokens", 10000)
            self.max_memories_per_injection = getattr(
                config, "max_memories_per_injection", 150
            )
            self.retrieval_importance_floor = getattr(
                config, "retrieval_importance_floor", 0.4
            )
            self.default_ttl = getattr(config, "default_ttl", 10)

    @property
    def _scope_filter(self):
        """Return (column, value) for scoping queries by project or job."""
        if self.project_scoped and self.project_ids:
            if len(self.project_ids) == 1:
                return "project_id", self.project_ids[0]
            return "project_id", self.project_ids
        if self.project_scoped and self.project_id:
            return "project_id", self.project_id
        return "job_id", self.job_id

    def _scope_where(self, param_index: int):
        """Return (WHERE clause fragment, param value) for scoping.

        Handles both single-value (= $N) and multi-value (= ANY($N)) scoping.
        """
        scope_col, scope_val = self._scope_filter
        if isinstance(scope_val, list):
            return f"{scope_col} = ANY(${param_index})", scope_val
        return f"{scope_col} = ${param_index}", scope_val

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
        remaining_turns: Optional[int] = None,
        retrieval_messages: Optional[List[str]] = None,
    ) -> Optional[uuid.UUID]:
        """Store a memory with automatic embedding and dedup.

        If a semantically similar memory already exists (cosine > dedup_threshold),
        updates the existing memory's access_count, last_accessed, and TTL instead.

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
            remaining_turns: TTL in turns (default: self.default_ttl)

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
                f"Dedup: updating existing memory {existing.id} instead of creating new"
            )
            ttl = remaining_turns if remaining_turns is not None else self.default_ttl
            await self.db.execute(
                """
                UPDATE memories
                SET access_count = access_count + 1,
                    last_accessed = CURRENT_TIMESTAMP,
                    importance = GREATEST(importance, $1),
                    remaining_turns = GREATEST(COALESCE(remaining_turns, 0), $3)
                WHERE id = $2
                """,
                importance,
                existing.id,
                ttl,
            )
            if self._archiver:
                self._archiver.audit_step(
                    job_id=str(self.job_id),
                    agent_type=self.agent_id or "",
                    step_type="memory_dedup",
                    node_name="recall_store",
                    iteration=0,
                    data={
                        "existing_id": str(existing.id),
                        "source": source,
                        "similarity": round(existing.similarity, 3)
                        if hasattr(existing, "similarity")
                        else None,
                    },
                )
            return existing.id

        # Estimate token count if not provided
        if token_count is None:
            # Rough estimate: ~4 chars per token
            token_count = len(content) // 4

        # Default TTL
        ttl = remaining_turns if remaining_turns is not None else self.default_ttl

        # Build tsvector for sparse search
        keywords_list = keywords or []
        keywords_text = " ".join(keywords_list) + " " + content

        mem_id = await self.db.fetchval(
            """
            INSERT INTO memories (
                job_id, project_id, agent_id, content, summary, memory_type, source,
                keywords, embedding, sparse_keywords,
                importance, source_turn_start, source_turn_end, source_phase,
                token_count, remaining_turns
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7,
                $8, $9, to_tsvector('english', $10),
                $11, $12, $13, $14,
                $15, $16
            )
            RETURNING id
            """,
            self.job_id,
            self.project_id,
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
            ttl,
        )

        # Store retrieval messages (trigger phrases) if provided
        if retrieval_messages and mem_id:
            try:
                rm_count = await self.store_retrieval_messages(
                    mem_id, retrieval_messages
                )
                logger.debug(
                    f"Stored {rm_count} retrieval messages for memory {mem_id}"
                )
            except Exception as e:
                logger.warning(f"Failed to store retrieval messages for {mem_id}: {e}")

        logger.debug(
            f"Stored memory {mem_id} (type={memory_type}, source={source}, "
            f"importance={importance})"
        )
        if self._archiver:
            self._archiver.audit_step(
                job_id=str(self.job_id),
                agent_type=self.agent_id or "",
                step_type="memory_store",
                node_name="recall_store",
                iteration=0,
                data={
                    "id": str(mem_id),
                    "type": memory_type,
                    "source": source,
                    "importance": importance,
                    "tokens": token_count,
                },
            )
        return mem_id

    async def store_retrieval_messages(
        self,
        memory_id: uuid.UUID,
        messages: List[str],
    ) -> int:
        """Store retrieval messages (trigger phrases) for a memory.

        Each message is embedded and stored so that hybrid search can match
        against "when would this memory be useful?" queries in addition to
        the memory's own content embedding.

        Args:
            memory_id: UUID of the parent memory
            messages: List of trigger phrase strings

        Returns:
            Number of messages successfully stored
        """
        if not messages:
            return 0

        # Batch embed all messages in one API call
        embeddings = await self.embedding_service.embed_batch(messages)

        stored = 0
        for msg, emb in zip(messages, embeddings):
            try:
                await self.db.execute(
                    """
                    INSERT INTO memory_retrieval_messages (memory_id, message, embedding)
                    VALUES ($1, $2, $3)
                    """,
                    memory_id,
                    msg,
                    emb,
                )
                stored += 1
            except Exception as e:
                logger.warning(
                    f"Failed to store retrieval message for {memory_id}: {e}"
                )

        return stored

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

        scope_clause, scope_val = self._scope_where(2)
        row = await self.db.fetchrow(
            f"""
            SELECT *, 1 - (embedding <=> $1) AS similarity
            FROM memories
            WHERE {scope_clause}
              AND embedding IS NOT NULL
              AND 1 - (embedding <=> $1) > $3
            ORDER BY similarity DESC
            LIMIT 1
            """,
            embedding,
            scope_val,
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

        scope_clause, scope_val = self._scope_where(1)
        rows = await self.db.fetch(
            f"""
            SELECT *
            FROM memories
            WHERE {scope_clause} AND embedding IS NOT NULL
            ORDER BY embedding <=> $2
            LIMIT $3
            """,
            scope_val,
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

        scope_clause, scope_val = self._scope_where(1)
        rows = await self.db.fetch(
            f"""
            SELECT *,
                   ts_rank_cd(sparse_keywords, websearch_to_tsquery('english', $2)) AS rank
            FROM memories
            WHERE {scope_clause}
              AND sparse_keywords @@ websearch_to_tsquery('english', $2)
            ORDER BY rank DESC
            LIMIT $3
            """,
            scope_val,
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

        scope_clause, scope_val = self._scope_where(1)
        rows = await self.db.fetch(
            f"""
            SELECT *
            FROM memories
            WHERE {scope_clause}
            ORDER BY created_at DESC
            LIMIT $2
            """,
            scope_val,
            limit,
        )

        return [MemoryRecord.from_row(dict(row)) for row in rows]

    # =========================================================================
    # TTL Management
    # =========================================================================

    async def get_ttl_active(self) -> List[MemoryRecord]:
        """Fetch all memories with remaining_turns > 0 (guaranteed injection).

        These memories bypass hybrid search relevance and are always included
        in the injection block until their TTL expires.

        Returns:
            List of TTL-active MemoryRecord objects ordered by importance
        """
        scope_clause, scope_val = self._scope_where(1)
        rows = await self.db.fetch(
            f"""
            SELECT *
            FROM memories
            WHERE {scope_clause} AND remaining_turns > 0
            ORDER BY importance DESC
            """,
            scope_val,
        )
        return [MemoryRecord.from_row(dict(row)) for row in rows]

    async def decrement_ttl(self) -> int:
        """Decrement remaining_turns for all TTL-active memories in scope.

        Called once per turn in the execute node, before memory retrieval.

        Returns:
            Number of memories whose TTL was decremented
        """
        scope_clause, scope_val = self._scope_where(1)
        result = await self.db.fetchval(
            f"""
            WITH updated AS (
                UPDATE memories
                SET remaining_turns = remaining_turns - 1
                WHERE {scope_clause} AND remaining_turns > 0
                RETURNING id
            )
            SELECT COUNT(*) FROM updated
            """,
            scope_val,
        )
        return result or 0

    async def boost_ttl(self, memory_id: uuid.UUID, turns: int) -> bool:
        """Increase remaining_turns for a specific memory.

        Used by the assembler to pin/extend memories that are currently relevant.

        Args:
            memory_id: UUID of the memory to boost
            turns: Number of turns to add

        Returns:
            True if the memory was found and updated
        """
        result = await self.db.execute(
            """
            UPDATE memories
            SET remaining_turns = COALESCE(remaining_turns, 0) + $1
            WHERE id = $2
            """,
            turns,
            memory_id,
        )
        return result != "UPDATE 0"

    async def deprecate_ttl(self, memory_id: uuid.UUID, turns: int) -> bool:
        """Decrease remaining_turns for a specific memory (floor at 0).

        Used by the assembler to fade memories that are no longer relevant.

        Args:
            memory_id: UUID of the memory to deprecate
            turns: Number of turns to subtract

        Returns:
            True if the memory was found and updated
        """
        result = await self.db.execute(
            """
            UPDATE memories
            SET remaining_turns = GREATEST(COALESCE(remaining_turns, 0) - $1, 0)
            WHERE id = $2
            """,
            turns,
            memory_id,
        )
        return result != "UPDATE 0"

    async def hybrid_search(
        self,
        query_text: str,
        query_embedding: List[float],
        match_count: Optional[int] = None,
        dense_weight: float = 0.6,
        sparse_weight: float = 0.3,
        recency_weight: float = 0.1,
        importance_floor: Optional[float] = None,
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
            importance_floor: Minimum importance to include (default: self.retrieval_importance_floor)

        Returns:
            List of MemoryRecord objects ranked by RRF score
        """
        match_count = match_count or self.max_memories_per_injection
        importance_floor = (
            importance_floor
            if importance_floor is not None
            else self.retrieval_importance_floor
        )

        if self.project_scoped and self.project_ids and len(self.project_ids) > 1:
            func_name = "memory_multi_project_hybrid_search"
            scope_val = self.project_ids
        elif self.project_scoped and self.project_ids:
            func_name = "memory_project_hybrid_search"
            scope_val = self.project_ids[0]
        elif self.project_scoped and self.project_id:
            func_name = "memory_project_hybrid_search"
            scope_val = self.project_id
        else:
            func_name = "memory_hybrid_search"
            scope_val = self.job_id

        rows = await self.db.fetch(
            f"""
            SELECT * FROM {func_name}(
                $1, $2, $3, $4, $5, $6, $7, importance_floor => $8
            )
            """,
            query_text,
            query_embedding,
            scope_val,
            match_count,
            dense_weight,
            sparse_weight,
            recency_weight,
            importance_floor,
        )

        # Update access tracking and reset TTL for accessed memories
        if rows:
            ids = [row["id"] for row in rows]
            await self.db.execute(
                """
                UPDATE memories
                SET access_count = access_count + 1,
                    last_accessed = CURRENT_TIMESTAMP,
                    remaining_turns = GREATEST(
                        COALESCE(remaining_turns, 0), $2
                    )
                WHERE id = ANY($1)
                """,
                ids,
                self.default_ttl,
            )

        results = [MemoryRecord.from_row(dict(row)) for row in rows]

        if self._archiver and results:
            self._archiver.audit_step(
                job_id=str(self.job_id),
                agent_type=self.agent_id or "",
                step_type="memory_retrieve",
                node_name="recall_store",
                iteration=0,
                data={
                    "count": len(results),
                    "total_tokens": sum(m.token_count for m in results),
                },
            )

        return results

    async def retrieve(
        self,
        context_text: str,
        budget_tokens: Optional[int] = None,
    ) -> List[MemoryRecord]:
        """High-level retrieval: two-tier (TTL-guaranteed + hybrid search), fit to budget.

        This is the main entry point for memory retrieval. It:
        1. Fetches TTL-active memories (guaranteed injection, remaining_turns > 0)
        2. Embeds context and runs hybrid search for the remaining budget
        3. Deduplicates and trims to fit within the token budget

        Args:
            context_text: Current context (e.g., current todo + recent messages)
            budget_tokens: Token budget for memory injection

        Returns:
            List of MemoryRecord objects that fit within budget.
            TTL-active (pinned) memories come first, then hybrid search results.
        """
        budget = budget_tokens or self.budget_tokens

        # Tier 1: TTL-active memories (guaranteed injection)
        pinned = await self.get_ttl_active()
        result = []
        tokens_used = 0
        pinned_ids = set()

        for memory in pinned:
            if tokens_used + memory.token_count > budget:
                logger.warning(
                    f"TTL-active memories exceed budget ({tokens_used} + "
                    f"{memory.token_count} > {budget}), truncating"
                )
                break
            result.append(memory)
            tokens_used += memory.token_count
            pinned_ids.add(memory.id)

        # Tier 2: Hybrid search with remaining budget
        remaining_budget = budget - tokens_used
        if remaining_budget > 0:
            context_embedding = await self.embedding_service.embed(context_text)
            candidates = await self.hybrid_search(
                query_text=context_text,
                query_embedding=context_embedding,
            )

            for memory in candidates:
                # Deduplicate against pinned memories
                if memory.id in pinned_ids:
                    continue
                if tokens_used + memory.token_count > budget:
                    break
                result.append(memory)
                tokens_used += memory.token_count

        pinned_count = len(pinned_ids & {m.id for m in result})
        retrieved_count = len(result) - pinned_count
        logger.debug(
            f"Retrieved {len(result)} memories ({tokens_used} tokens): "
            f"{pinned_count} pinned + {retrieved_count} from hybrid search "
            f"(budget={budget})"
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
        if memory.remaining_turns is not None and memory.remaining_turns > 0:
            meta_parts.append(f"pinned, {memory.remaining_turns} turns left")
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
        budget_tokens: int = 10000,
    ) -> str:
        """Assemble formatted memory block for injection.

        Separates TTL-active (pinned) memories from retrieval-based memories
        for clear visual distinction.

        Args:
            memories: List of MemoryRecord objects (pinned first, then retrieved)
            budget_tokens: Token budget (for display in footer)

        Returns:
            Formatted memory block string
        """
        if not memories:
            return ""

        pinned = [m for m in memories if m.remaining_turns and m.remaining_turns > 0]
        retrieved = [
            m for m in memories if not (m.remaining_turns and m.remaining_turns > 0)
        ]
        tokens_used = sum(m.token_count for m in memories)

        lines = []
        idx = 1

        if pinned:
            lines.append("--- Pinned Memories (TTL-active) ---")
            lines.append("")
            for memory in pinned:
                lines.append(cls.format_memory(memory, idx))
                lines.append("")
                idx += 1

        if retrieved:
            lines.append("--- Retrieved Memories (relevance-ranked) ---")
            lines.append("")
            for memory in retrieved:
                lines.append(cls.format_memory(memory, idx))
                lines.append("")
                idx += 1

        lines.append(
            f"--- End Memories ({len(memories)} items: "
            f"{len(pinned)} pinned + {len(retrieved)} retrieved, "
            f"~{tokens_used:,} tokens) ---"
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
        scope_clause, scope_val = self._scope_where(1)
        row = await self.db.fetchrow(
            f"""
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
                AVG(importance) AS avg_importance,
                COUNT(*) FILTER (WHERE remaining_turns > 0) AS ttl_active
            FROM memories
            WHERE {scope_clause}
            """,
            scope_val,
        )

        if row:
            return dict(row)
        return {"total": 0, "total_tokens": 0}
