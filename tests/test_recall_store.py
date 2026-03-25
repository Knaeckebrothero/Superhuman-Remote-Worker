"""Unit and integration tests for RecallStore.

Unit tests use mocked db/embedding and run everywhere.
Integration tests require DATABASE_URL and are skipped otherwise.
"""

import os
import uuid
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.recall_store import MemoryRecord, RecallStore


# =============================================================================
# Fixtures
# =============================================================================


@dataclass
class MockMemoryConfig:
    """Minimal config matching MemoryConfig fields."""

    enabled: bool = True
    budget_tokens: int = 5000
    max_memories_per_injection: int = 10
    dense_results: int = 5
    sparse_results: int = 5
    recent_results: int = 3
    importance_threshold: float = 0.3
    dedup_threshold: float = 0.92


@pytest.fixture
def job_id():
    return uuid.uuid4()


@pytest.fixture
def mock_embedding_service():
    service = MagicMock()
    service.embed = AsyncMock(return_value=[0.1] * 1536)
    service.embed_batch = AsyncMock(return_value=[[0.1] * 1536])
    return service


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.fetchval = AsyncMock()
    db.fetchrow = AsyncMock()
    db.fetch = AsyncMock(return_value=[])
    db.execute = AsyncMock()
    return db


@pytest.fixture
def config():
    return MockMemoryConfig()


@pytest.fixture
def store(mock_db, mock_embedding_service, job_id, config):
    return RecallStore(
        db=mock_db,
        embedding_service=mock_embedding_service,
        job_id=job_id,
        config=config,
    )


# =============================================================================
# MemoryRecord Tests
# =============================================================================


class TestMemoryRecord:
    """Test MemoryRecord dataclass."""

    def test_from_row(self):
        """from_row creates a record from a dict."""
        row = {
            "id": uuid.uuid4(),
            "job_id": uuid.uuid4(),
            "content": "test content",
            "summary": "test",
            "memory_type": "procedural",
            "source": "todo",
            "keywords": ["a", "b"],
            "importance": 0.8,
            "token_count": 42,
            "access_count": 3,
            "source_phase": 2,
        }
        record = MemoryRecord.from_row(row)
        assert record.content == "test content"
        assert record.memory_type == "procedural"
        assert record.source == "todo"
        assert record.keywords == ["a", "b"]
        assert record.importance == 0.8
        assert record.token_count == 42

    def test_from_row_defaults(self):
        """from_row handles missing fields gracefully."""
        record = MemoryRecord.from_row({})
        assert record.content == ""
        assert record.memory_type == "factual"
        assert record.source == "observer"
        assert record.keywords == []
        assert record.importance == 0.5

    def test_from_row_none_keywords(self):
        """from_row converts None keywords to empty list."""
        record = MemoryRecord.from_row({"keywords": None})
        assert record.keywords == []


# =============================================================================
# Storage Tests
# =============================================================================


class TestStore:
    """Test RecallStore.store()."""

    @pytest.mark.asyncio
    async def test_store_basic(self, store, mock_db, mock_embedding_service):
        """store() embeds content and inserts into database."""
        mock_db.fetchrow.return_value = None  # No duplicates
        mock_db.fetchval.return_value = uuid.uuid4()

        mem_id = await store.store(
            content="The API uses JWT tokens for auth.",
            summary="API uses JWT auth",
            keywords=["jwt", "auth", "api"],
            importance=0.8,
        )

        assert mem_id is not None
        mock_embedding_service.embed.assert_awaited_once_with(
            "The API uses JWT tokens for auth."
        )
        mock_db.fetchval.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_store_below_threshold(self, store, mock_embedding_service):
        """store() skips memories below importance threshold."""
        result = await store.store(
            content="Unimportant note",
            importance=0.1,  # Below 0.3 threshold
        )

        assert result is None
        mock_embedding_service.embed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_store_dedup_updates_existing(
        self, store, mock_db, mock_embedding_service
    ):
        """store() updates existing memory when duplicate found."""
        existing_id = uuid.uuid4()
        # find_similar returns a match
        mock_db.fetchrow.return_value = {
            "id": existing_id,
            "content": "Similar content",
            "similarity": 0.95,
            "job_id": store.job_id,
            "memory_type": "factual",
            "source": "observer",
            "importance": 0.7,
            "token_count": 10,
            "access_count": 1,
        }

        result = await store.store(
            content="Similar content rephrased",
            importance=0.9,
        )

        assert result == existing_id
        # Should update existing, not insert
        mock_db.execute.assert_awaited()

    @pytest.mark.asyncio
    async def test_store_estimates_tokens(self, store, mock_db):
        """store() estimates token count when not provided."""
        mock_db.fetchrow.return_value = None  # No duplicates
        mock_db.fetchval.return_value = uuid.uuid4()

        await store.store(
            content="A" * 400,  # ~100 tokens
            importance=0.5,
        )

        # Check the token_count arg (15th positional arg, $15 with project_id added)
        call_args = mock_db.fetchval.call_args
        token_count_arg = call_args[0][15]  # $15 = token_count
        assert token_count_arg == 100  # 400 chars / 4

    @pytest.mark.asyncio
    async def test_store_truncates_summary(self, store, mock_db):
        """store() truncates summary to 500 chars."""
        mock_db.fetchrow.return_value = None
        mock_db.fetchval.return_value = uuid.uuid4()

        await store.store(
            content="content",
            summary="x" * 600,
            importance=0.5,
        )

        call_args = mock_db.fetchval.call_args
        summary_arg = call_args[0][5]  # $5 = summary (shifted by project_id)
        assert len(summary_arg) == 500


# =============================================================================
# Retrieval Tests
# =============================================================================


class TestRetrieval:
    """Test RecallStore retrieval methods."""

    @pytest.mark.asyncio
    async def test_search_dense(self, store, mock_db):
        """search_dense() queries by vector similarity."""
        mock_db.fetch.return_value = [
            {
                "id": uuid.uuid4(),
                "job_id": store.job_id,
                "content": "Memory 1",
                "memory_type": "factual",
                "source": "observer",
                "importance": 0.9,
                "token_count": 20,
                "access_count": 0,
                "keywords": ["test"],
            }
        ]

        results = await store.search_dense([0.1] * 1536)
        assert len(results) == 1
        assert results[0].content == "Memory 1"

    @pytest.mark.asyncio
    async def test_search_sparse(self, store, mock_db):
        """search_sparse() queries by tsvector matching."""
        mock_db.fetch.return_value = [
            {
                "id": uuid.uuid4(),
                "job_id": store.job_id,
                "content": "JWT auth pattern",
                "memory_type": "procedural",
                "source": "todo",
                "importance": 0.7,
                "token_count": 15,
                "access_count": 1,
                "keywords": ["jwt", "auth"],
                "rank": 0.5,
            }
        ]

        results = await store.search_sparse("JWT authentication")
        assert len(results) == 1
        assert results[0].memory_type == "procedural"

    @pytest.mark.asyncio
    async def test_get_recent(self, store, mock_db):
        """get_recent() returns most recently created memories."""
        mock_db.fetch.return_value = [
            {
                "id": uuid.uuid4(),
                "job_id": store.job_id,
                "content": "Recent memory",
                "memory_type": "factual",
                "source": "observer",
                "importance": 0.5,
                "token_count": 10,
                "access_count": 0,
                "keywords": [],
            }
        ]

        results = await store.get_recent(limit=3)
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_retrieve_respects_budget(
        self, store, mock_db, mock_embedding_service
    ):
        """retrieve() stops adding memories when budget is exhausted."""
        # Return 3 candidates with 2000 tokens each
        mock_db.fetch.return_value = [
            {
                "id": uuid.uuid4(),
                "job_id": store.job_id,
                "content": f"Memory {i}",
                "memory_type": "factual",
                "source": "observer",
                "importance": 0.9 - i * 0.1,
                "token_count": 2000,
                "access_count": 0,
                "keywords": [],
            }
            for i in range(3)
        ]

        store.budget_tokens = 5000
        results = await store.retrieve("test context")

        # Should fit 2 memories (4000 tokens), not 3 (6000 would exceed 5000)
        assert len(results) == 2


# =============================================================================
# Assembly Tests
# =============================================================================


class TestAssembly:
    """Test memory block assembly."""

    def test_format_memory_basic(self):
        """format_memory() creates readable output."""
        memory = MemoryRecord(
            content="The users table uses soft deletes.",
            importance=0.9,
            source_phase=2,
        )

        result = RecallStore.format_memory(memory, 1)
        assert "[1]" in result
        assert "importance: 0.9" in result
        assert "phase 2" in result
        assert "The users table uses soft deletes." in result

    def test_format_memory_with_type(self):
        """format_memory() shows non-default memory type."""
        memory = MemoryRecord(
            content="Always use async/await.",
            importance=0.7,
            memory_type="procedural",
        )

        result = RecallStore.format_memory(memory, 1)
        assert "procedural" in result

    def test_format_memory_factual_type_hidden(self):
        """format_memory() hides default 'factual' type."""
        memory = MemoryRecord(
            content="Some fact.",
            importance=0.5,
            memory_type="factual",
        )

        result = RecallStore.format_memory(memory, 1)
        assert "factual" not in result

    def test_assemble_memory_block(self):
        """assemble_memory_block() creates full block with header/footer."""
        memories = [
            MemoryRecord(content="Memory one", importance=0.9, token_count=100),
            MemoryRecord(content="Memory two", importance=0.7, token_count=50),
        ]

        block = RecallStore.assemble_memory_block(memories)
        assert "--- Retrieved Memories (relevance-ranked) ---" in block
        assert (
            "--- End Memories (2 items: 0 pinned + 2 retrieved, ~150 tokens) ---"
            in block
        )
        assert "[1]" in block
        assert "[2]" in block
        assert "Memory one" in block
        assert "Memory two" in block

    def test_assemble_memory_block_empty(self):
        """assemble_memory_block() returns empty string for no memories."""
        assert RecallStore.assemble_memory_block([]) == ""

    def test_format_memory_pinned(self):
        """format_memory() shows pinned status with TTL."""
        memory = MemoryRecord(
            content="Important pinned memory.",
            importance=0.9,
            remaining_turns=7,
        )

        result = RecallStore.format_memory(memory, 1)
        assert "pinned, 7 turns left" in result
        assert "importance: 0.9" in result

    def test_assemble_memory_block_mixed(self):
        """assemble_memory_block() separates pinned from retrieved."""
        memories = [
            MemoryRecord(
                content="Pinned one", importance=0.9, token_count=50, remaining_turns=5
            ),
            MemoryRecord(
                content="Retrieved one",
                importance=0.7,
                token_count=50,
                remaining_turns=0,
            ),
            MemoryRecord(content="Retrieved two", importance=0.5, token_count=50),
        ]

        block = RecallStore.assemble_memory_block(memories)
        assert "--- Pinned Memories (TTL-active) ---" in block
        assert "--- Retrieved Memories (relevance-ranked) ---" in block
        assert "1 pinned + 2 retrieved" in block
        assert "Pinned one" in block
        assert "Retrieved one" in block
        assert "Retrieved two" in block

    def test_memory_record_remaining_turns(self):
        """MemoryRecord includes remaining_turns from row."""
        row = {
            "content": "Test memory",
            "remaining_turns": 5,
        }
        record = MemoryRecord.from_row(row)
        assert record.remaining_turns == 5

    def test_memory_record_remaining_turns_null(self):
        """MemoryRecord handles None remaining_turns."""
        row = {"content": "Test memory"}
        record = MemoryRecord.from_row(row)
        assert record.remaining_turns is None


# =============================================================================
# Stats Tests
# =============================================================================


class TestStats:
    """Test get_stats()."""

    @pytest.mark.asyncio
    async def test_get_stats(self, store, mock_db):
        """get_stats() returns aggregated statistics."""
        mock_db.fetchrow.return_value = {
            "total": 25,
            "total_tokens": 12500,
            "total_accesses": 80,
            "factual": 10,
            "procedural": 5,
            "error_solution": 3,
            "vocabulary": 2,
            "relational": 5,
            "from_observer": 12,
            "from_todo": 8,
            "from_compaction": 3,
            "from_phase_archive": 1,
            "from_tool_error": 1,
            "avg_importance": 0.65,
        }

        stats = await store.get_stats()
        assert stats["total"] == 25
        assert stats["total_tokens"] == 12500
        assert stats["from_observer"] == 12
        assert stats["avg_importance"] == 0.65

    @pytest.mark.asyncio
    async def test_get_stats_empty(self, store, mock_db):
        """get_stats() handles empty memory store."""
        mock_db.fetchrow.return_value = None

        stats = await store.get_stats()
        assert stats["total"] == 0


# =============================================================================
# Integration Tests (require DATABASE_URL)
# =============================================================================

HAS_DATABASE = bool(os.getenv("DATABASE_URL"))


@pytest.mark.skipif(not HAS_DATABASE, reason="DATABASE_URL not set")
class TestIntegration:
    """Integration tests requiring a live PostgreSQL database with pgvector."""

    @pytest.fixture
    async def live_db(self):
        """Create a live database connection."""
        from src.database.postgres_db import PostgresDB

        db = PostgresDB()
        await db.connect()

        # Ensure schema exists
        async with db.acquire() as conn:
            schema_path = (
                __import__("pathlib").Path(__file__).parent.parent
                / "src"
                / "database"
                / "queries"
                / "postgres"
                / "schema.sql"
            )
            await conn.execute(schema_path.read_text())

        yield db
        await db.close()

    @pytest.fixture
    async def live_store(self, live_db):
        """Create a RecallStore with live database."""
        from src.services.embedding_service import get_embedding_service

        job_id = uuid.uuid4()

        # Create a test job first
        await live_db.execute(
            "INSERT INTO jobs (id, description, status) VALUES ($1, $2, $3)",
            job_id,
            "Integration test job",
            "created",
        )

        store = RecallStore(
            db=live_db,
            embedding_service=get_embedding_service(),
            job_id=job_id,
            config=MockMemoryConfig(),
        )

        yield store

        # Cleanup
        await live_db.execute("DELETE FROM memories WHERE job_id = $1", job_id)
        await live_db.execute("DELETE FROM jobs WHERE id = $1", job_id)

    @pytest.mark.asyncio
    async def test_integration_store_and_retrieve(self, live_store):
        """Store a memory and retrieve it via hybrid search."""
        mem_id = await live_store.store(
            content="The config system uses YAML with $extends inheritance.",
            summary="Config uses YAML inheritance",
            keywords=["config", "yaml", "extends", "inheritance"],
            importance=0.8,
            memory_type="factual",
            source="observer",
        )

        assert mem_id is not None

        # Retrieve via hybrid search
        memories = await live_store.retrieve("How does config inheritance work?")
        assert len(memories) > 0
        assert any("config" in m.content.lower() for m in memories)

    @pytest.mark.asyncio
    async def test_integration_dedup(self, live_store):
        """Dedup prevents storing identical memories."""
        id1 = await live_store.store(
            content="PostgreSQL uses asyncpg for async connections.",
            importance=0.7,
        )
        id2 = await live_store.store(
            content="PostgreSQL uses asyncpg for async connections.",
            importance=0.8,
        )

        # Both should return the same ID (dedup)
        assert id1 == id2

    @pytest.mark.asyncio
    async def test_integration_stats(self, live_store):
        """Stats correctly count stored memories."""
        await live_store.store(
            content="Fact one about the system.",
            importance=0.6,
            memory_type="factual",
            source="todo",
        )
        await live_store.store(
            content="How to configure the agent properly.",
            importance=0.7,
            memory_type="procedural",
            source="observer",
        )

        stats = await live_store.get_stats()
        assert stats["total"] >= 2
