"""Unit tests for vector search functionality (Phase 8).

Tests cover:
- VectorConfig configuration
- EmbeddingManager text chunking and embedding (mocked)
- WorkspaceVectorStore operations (mocked database)
- VectorizedWorkspaceManager wrapper
- Vector tools (semantic_search, index_file_for_search)
"""

import asyncio
import pytest
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

# Import vector modules
from src.agents.shared.vector import (
    VectorConfig,
    EmbeddingManager,
    WorkspaceVectorStore,
    VectorizedWorkspaceManager,
    create_workspace_vector_store,
    EMBEDDING_DIMENSIONS,
)
from src.agents.shared.workspace_manager import WorkspaceManager


class TestVectorConfig:
    """Tests for VectorConfig dataclass."""

    def test_default_values(self):
        """Test default configuration values."""
        config = VectorConfig()

        assert config.embedding_model == "text-embedding-3-small"
        assert config.embedding_dimensions == 1536
        assert config.chunk_size == 1000
        assert config.chunk_overlap == 200
        assert config.default_top_k == 10
        assert config.similarity_threshold == 0.7
        assert config.min_content_length == 50
        assert ".pdf" in config.skip_extensions
        assert ".docx" in config.skip_extensions

    def test_custom_values(self):
        """Test custom configuration values."""
        config = VectorConfig(
            embedding_model="text-embedding-3-large",
            embedding_dimensions=3072,
            chunk_size=2000,
            similarity_threshold=0.8,
        )

        assert config.embedding_model == "text-embedding-3-large"
        assert config.embedding_dimensions == 3072
        assert config.chunk_size == 2000
        assert config.similarity_threshold == 0.8

    def test_embedding_dimensions_constant(self):
        """Test embedding dimensions are defined for known models."""
        assert "text-embedding-3-small" in EMBEDDING_DIMENSIONS
        assert "text-embedding-3-large" in EMBEDDING_DIMENSIONS
        assert EMBEDDING_DIMENSIONS["text-embedding-3-small"] == 1536
        assert EMBEDDING_DIMENSIONS["text-embedding-3-large"] == 3072


class TestEmbeddingManager:
    """Tests for EmbeddingManager class."""

    def test_initialization(self):
        """Test embedding manager initialization."""
        config = VectorConfig(api_key="test-key")
        manager = EmbeddingManager(config=config)

        assert manager.config.api_key == "test-key"
        assert manager._client is None  # Lazy initialization

    def test_chunk_text_short(self):
        """Test chunking short text (no splitting needed)."""
        manager = EmbeddingManager(VectorConfig(chunk_size=1000))

        text = "This is a short text."
        chunks = manager.chunk_text(text)

        assert len(chunks) == 1
        assert chunks[0] == (0, text)

    def test_chunk_text_long(self):
        """Test chunking long text into multiple chunks."""
        manager = EmbeddingManager(VectorConfig(chunk_size=100, chunk_overlap=20))

        # Create text longer than chunk_size
        text = " ".join(["word"] * 100)  # ~500 chars
        chunks = manager.chunk_text(text)

        assert len(chunks) > 1
        # Verify chunk indices are sequential
        for i, (idx, _) in enumerate(chunks):
            assert idx == i

    def test_chunk_text_sentence_boundary(self):
        """Test chunking respects sentence boundaries when possible."""
        manager = EmbeddingManager(VectorConfig(chunk_size=100, chunk_overlap=20))

        text = "First sentence. Second sentence. Third sentence. Fourth sentence."
        chunks = manager.chunk_text(text)

        # Check that chunks don't break mid-word
        for _, chunk in chunks:
            assert not chunk.startswith(" ")
            assert chunk.strip() == chunk

    @pytest.mark.asyncio
    async def test_embed_text_mocked(self):
        """Test embedding text with mocked OpenAI client."""
        manager = EmbeddingManager(VectorConfig(api_key="test-key"))

        # Mock the client
        mock_response = MagicMock()
        mock_response.data = [MagicMock(embedding=[0.1] * 1536)]

        mock_client = AsyncMock()
        mock_client.embeddings.create = AsyncMock(return_value=mock_response)
        manager._client = mock_client

        embedding = await manager.embed_text("test text")

        assert len(embedding) == 1536
        mock_client.embeddings.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_embed_texts_mocked(self):
        """Test embedding multiple texts with mocked client."""
        manager = EmbeddingManager(VectorConfig(api_key="test-key"))

        # Mock the client
        mock_response = MagicMock()
        mock_response.data = [
            MagicMock(embedding=[0.1] * 1536),
            MagicMock(embedding=[0.2] * 1536),
        ]

        mock_client = AsyncMock()
        mock_client.embeddings.create = AsyncMock(return_value=mock_response)
        manager._client = mock_client

        embeddings = await manager.embed_texts(["text1", "text2"])

        assert len(embeddings) == 2
        assert len(embeddings[0]) == 1536
        assert len(embeddings[1]) == 1536

    @pytest.mark.asyncio
    async def test_embed_texts_empty(self):
        """Test embedding empty list returns empty list."""
        manager = EmbeddingManager(VectorConfig(api_key="test-key"))

        embeddings = await manager.embed_texts([])

        assert embeddings == []


class TestWorkspaceVectorStore:
    """Tests for WorkspaceVectorStore class."""

    def test_initialization(self):
        """Test vector store initialization."""
        store = WorkspaceVectorStore(
            job_id="test-job",
            connection_string="postgresql://test:test@localhost/test",
        )

        assert store.job_id == "test-job"
        assert store.connection_string is not None
        assert not store._initialized

    def test_initialization_requires_connection(self):
        """Test that connection string is required."""
        # Clear any env var that might be set
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ValueError, match="connection string required"):
                WorkspaceVectorStore(job_id="test-job", connection_string=None)

    def test_should_index_text_file(self):
        """Test _should_index for valid text files."""
        store = WorkspaceVectorStore(
            job_id="test-job",
            connection_string="postgresql://test:test@localhost/test",
        )

        # Content must be at least 50 chars (min_content_length default)
        long_content = "This is some content to index that is long enough to be indexed properly."

        # Should index markdown files
        assert store._should_index("research.md", long_content)

        # Should index text files
        assert store._should_index("data/info.txt", long_content)

    def test_should_index_skip_binary(self):
        """Test _should_index skips binary files."""
        store = WorkspaceVectorStore(
            job_id="test-job",
            connection_string="postgresql://test:test@localhost/test",
        )

        # Should skip PDF files
        assert not store._should_index("docs/file.pdf", "content")

        # Should skip images
        assert not store._should_index("images/photo.png", "content")

    def test_should_index_skip_short(self):
        """Test _should_index skips short content."""
        store = WorkspaceVectorStore(
            job_id="test-job",
            connection_string="postgresql://test:test@localhost/test",
            config=VectorConfig(min_content_length=50),
        )

        # Should skip very short content
        assert not store._should_index("short.txt", "tiny")

    def test_compute_hash(self):
        """Test content hashing for change detection."""
        store = WorkspaceVectorStore(
            job_id="test-job",
            connection_string="postgresql://test:test@localhost/test",
        )

        hash1 = store._compute_hash("content A")
        hash2 = store._compute_hash("content B")
        hash3 = store._compute_hash("content A")

        # Same content should produce same hash
        assert hash1 == hash3
        # Different content should produce different hash
        assert hash1 != hash2
        # Hash should be 64 chars (SHA256 hex)
        assert len(hash1) == 64

    @pytest.mark.asyncio
    async def test_index_file_mocked(self):
        """Test file indexing with mocked database."""
        store = WorkspaceVectorStore(
            job_id="test-job",
            connection_string="postgresql://test:test@localhost/test",
        )

        # Mock database pool
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(return_value=None)  # No existing content
        mock_conn.execute = AsyncMock()

        mock_pool = AsyncMock()
        mock_pool.acquire = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_conn), __aexit__=AsyncMock()))

        store._pool = mock_pool
        store._initialized = True

        # Mock embedding manager
        mock_embeddings = [[0.1] * 1536]
        store._embedding_manager = MagicMock()
        store._embedding_manager.chunk_text = MagicMock(return_value=[(0, "test content")])
        store._embedding_manager.embed_texts = AsyncMock(return_value=mock_embeddings)

        # Index a file
        content = "This is test content for indexing that is long enough."
        chunks = await store.index_file("notes/test.md", content)

        assert chunks == 1

    @pytest.mark.asyncio
    async def test_search_mocked(self):
        """Test search with mocked database."""
        store = WorkspaceVectorStore(
            job_id="test-job",
            connection_string="postgresql://test:test@localhost/test",
        )

        # Mock database pool
        mock_rows = [
            {"file_path": "notes/test.md", "chunk_index": 0, "content_preview": "test content", "similarity": 0.85}
        ]
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=mock_rows)

        mock_pool = AsyncMock()
        mock_pool.acquire = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_conn), __aexit__=AsyncMock()))

        store._pool = mock_pool
        store._initialized = True

        # Mock embedding manager
        store._embedding_manager = MagicMock()
        store._embedding_manager.embed_text = AsyncMock(return_value=[0.1] * 1536)

        # Search
        results = await store.search("test query")

        assert len(results) == 1
        assert results[0]["file_path"] == "notes/test.md"
        assert results[0]["similarity"] == 0.85

    @pytest.mark.asyncio
    async def test_cleanup_job_mocked(self):
        """Test job cleanup with mocked database."""
        store = WorkspaceVectorStore(
            job_id="test-job",
            connection_string="postgresql://test:test@localhost/test",
        )

        # Mock database pool
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="DELETE 5")

        mock_pool = AsyncMock()
        mock_pool.acquire = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_conn), __aexit__=AsyncMock()))

        store._pool = mock_pool
        store._initialized = True

        # Cleanup
        deleted = await store.cleanup_job()

        assert deleted == 5


class TestVectorizedWorkspaceManager:
    """Tests for VectorizedWorkspaceManager wrapper."""

    def test_initialization(self):
        """Test VectorizedWorkspaceManager initialization."""
        with TemporaryDirectory() as tmpdir:
            workspace = WorkspaceManager(job_id="test-job", base_path=tmpdir)
            workspace.initialize()

            vectorized = VectorizedWorkspaceManager(workspace, auto_index=True)

            assert vectorized.job_id == "test-job"
            assert vectorized.workspace == workspace
            assert vectorized.vector_store is None
            assert vectorized._auto_index is True

    def test_delegate_read_file(self):
        """Test that read_file is delegated to workspace manager."""
        with TemporaryDirectory() as tmpdir:
            workspace = WorkspaceManager(job_id="test-job", base_path=tmpdir)
            workspace.initialize()

            # Write a file directly
            workspace.write_file("test.txt", "Hello World")

            vectorized = VectorizedWorkspaceManager(workspace)

            # Read through vectorized manager
            content = vectorized.read_file("test.txt")
            assert content == "Hello World"

    def test_delegate_list_files(self):
        """Test that list_files is delegated to workspace manager."""
        with TemporaryDirectory() as tmpdir:
            workspace = WorkspaceManager(job_id="test-job", base_path=tmpdir)
            workspace.initialize()

            workspace.write_file("file1.txt", "content1")
            workspace.write_file("file2.txt", "content2")

            vectorized = VectorizedWorkspaceManager(workspace)

            files = vectorized.list_files()
            assert "file1.txt" in files
            assert "file2.txt" in files

    def test_write_file_without_vector_store(self):
        """Test write_file works without vector store."""
        with TemporaryDirectory() as tmpdir:
            workspace = WorkspaceManager(job_id="test-job", base_path=tmpdir)
            workspace.initialize()

            vectorized = VectorizedWorkspaceManager(workspace, auto_index=True)

            # Should not raise even without vector store
            vectorized.write_file("test.txt", "content")

            # Verify file was written
            content = vectorized.read_file("test.txt")
            assert content == "content"

    @pytest.mark.asyncio
    async def test_write_file_async_with_indexing(self):
        """Test write_file_async with mocked vector store."""
        with TemporaryDirectory() as tmpdir:
            workspace = WorkspaceManager(job_id="test-job", base_path=tmpdir)
            workspace.initialize()

            # Mock vector store
            mock_vector_store = AsyncMock()
            mock_vector_store.index_file = AsyncMock(return_value=1)

            vectorized = VectorizedWorkspaceManager(
                workspace,
                vector_store=mock_vector_store,
                auto_index=True,
            )

            # Write file asynchronously
            await vectorized.write_file_async("notes/test.md", "Some content for indexing")

            # Verify indexing was called
            mock_vector_store.index_file.assert_called_once_with("notes/test.md", "Some content for indexing")

    @pytest.mark.asyncio
    async def test_delete_file_async_with_cleanup(self):
        """Test delete_file_async removes vector embeddings."""
        with TemporaryDirectory() as tmpdir:
            workspace = WorkspaceManager(job_id="test-job", base_path=tmpdir)
            workspace.initialize()
            workspace.write_file("test.txt", "content")

            # Mock vector store
            mock_vector_store = AsyncMock()
            mock_vector_store.delete_file = AsyncMock(return_value=1)

            vectorized = VectorizedWorkspaceManager(
                workspace,
                vector_store=mock_vector_store,
            )

            # Delete file
            result = await vectorized.delete_file_async("test.txt")

            assert result is True
            mock_vector_store.delete_file.assert_called_once_with("test.txt")

    @pytest.mark.asyncio
    async def test_semantic_search(self):
        """Test semantic_search delegates to vector store."""
        with TemporaryDirectory() as tmpdir:
            workspace = WorkspaceManager(job_id="test-job", base_path=tmpdir)
            workspace.initialize()

            # Mock vector store
            mock_results = [
                {"file_path": "notes/test.md", "chunk_index": 0, "content_preview": "test", "similarity": 0.9}
            ]
            mock_vector_store = AsyncMock()
            mock_vector_store.search = AsyncMock(return_value=mock_results)

            vectorized = VectorizedWorkspaceManager(
                workspace,
                vector_store=mock_vector_store,
            )

            # Search
            results = await vectorized.semantic_search("test query")

            assert len(results) == 1
            assert results[0]["file_path"] == "notes/test.md"
            mock_vector_store.search.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup(self):
        """Test cleanup removes both workspace and vector embeddings."""
        with TemporaryDirectory() as tmpdir:
            workspace = WorkspaceManager(job_id="test-job", base_path=tmpdir)
            workspace.initialize()
            workspace.write_file("test.txt", "content")

            # Mock vector store
            mock_vector_store = AsyncMock()
            mock_vector_store.cleanup_job = AsyncMock(return_value=5)

            vectorized = VectorizedWorkspaceManager(
                workspace,
                vector_store=mock_vector_store,
            )

            # Cleanup
            result = await vectorized.cleanup()

            assert result is True
            mock_vector_store.cleanup_job.assert_called_once()


class TestVectorTools:
    """Tests for vector tools."""

    def test_tools_require_vector_store(self):
        """Test that vector tools require vector_store in context."""
        from src.agents.shared.tools.context import ToolContext
        from src.agents.shared.tools.vector_tools import create_vector_tools

        with TemporaryDirectory() as tmpdir:
            workspace = WorkspaceManager(job_id="test-job", base_path=tmpdir)
            workspace.initialize()

            context = ToolContext(workspace_manager=workspace)

            with pytest.raises(ValueError, match="vector_store"):
                create_vector_tools(context)

    def test_tools_creation_with_vector_store(self):
        """Test that vector tools are created with vector_store."""
        from src.agents.shared.tools.context import ToolContext
        from src.agents.shared.tools.vector_tools import create_vector_tools

        with TemporaryDirectory() as tmpdir:
            workspace = WorkspaceManager(job_id="test-job", base_path=tmpdir)
            workspace.initialize()

            mock_vector_store = MagicMock()

            context = ToolContext(
                workspace_manager=workspace,
                vector_store=mock_vector_store,
            )

            tools = create_vector_tools(context)

            assert len(tools) == 3
            tool_names = {t.name for t in tools}
            assert "semantic_search" in tool_names
            assert "index_file_for_search" in tool_names
            assert "get_vector_index_stats" in tool_names


class TestToolRegistry:
    """Tests for vector tools in the registry."""

    def test_vector_tools_registered(self):
        """Test that vector tools are in the registry."""
        from src.agents.shared.tools.registry import TOOL_REGISTRY

        assert "semantic_search" in TOOL_REGISTRY
        assert "index_file_for_search" in TOOL_REGISTRY
        assert "get_vector_index_stats" in TOOL_REGISTRY

    def test_vector_tools_metadata(self):
        """Test vector tool metadata is correct."""
        from src.agents.shared.tools.registry import TOOL_REGISTRY

        semantic_search = TOOL_REGISTRY["semantic_search"]
        assert semantic_search["category"] == "workspace"
        assert "semantic" in semantic_search["description"].lower()


class TestPackageExports:
    """Tests for package exports."""

    def test_shared_exports_vector_classes(self):
        """Test that shared package exports vector classes."""
        from src.agents.shared import (
            VectorConfig,
            EmbeddingManager,
            WorkspaceVectorStore,
            VectorizedWorkspaceManager,
            create_workspace_vector_store,
        )

        # Just verify they're importable
        assert VectorConfig is not None
        assert EmbeddingManager is not None
        assert WorkspaceVectorStore is not None
        assert VectorizedWorkspaceManager is not None
        assert create_workspace_vector_store is not None

    def test_tools_export_create_vector_tools(self):
        """Test that tools package exports create_vector_tools."""
        from src.agents.shared.tools import create_vector_tools

        assert create_vector_tools is not None

    def test_tool_context_has_vector_store(self):
        """Test that ToolContext has vector_store field."""
        from src.agents.shared.tools import ToolContext

        context = ToolContext()
        assert hasattr(context, "vector_store")
        assert context.has_vector_store() is False

        # Set vector store
        mock_store = MagicMock()
        context.vector_store = mock_store
        assert context.has_vector_store() is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
