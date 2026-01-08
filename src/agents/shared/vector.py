"""Vector search for workspace files.

Provides semantic search capabilities over workspace contents using
pgvector for storage and OpenAI embeddings for vectorization.

Key components:
- EmbeddingManager: Creates text embeddings using OpenAI API
- WorkspaceVectorStore: PostgreSQL/pgvector storage and retrieval
- VectorConfig: Configuration for vector operations
"""

import asyncio
import hashlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Default embedding dimensions for OpenAI models
EMBEDDING_DIMENSIONS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}


@dataclass
class VectorConfig:
    """Configuration for vector operations."""

    # Embedding settings
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    api_key: Optional[str] = None  # Falls back to OPENAI_API_KEY env var
    base_url: Optional[str] = None  # Optional custom endpoint

    # Chunking settings
    chunk_size: int = 1000  # Characters per chunk
    chunk_overlap: int = 200  # Overlap between chunks

    # Search settings
    default_top_k: int = 10
    similarity_threshold: float = 0.7

    # Database settings
    connection_string: Optional[str] = None  # Falls back to DATABASE_URL env var

    # Content filtering
    min_content_length: int = 50  # Skip very short content
    skip_extensions: List[str] = field(
        default_factory=lambda: [".pdf", ".docx", ".png", ".jpg", ".gif", ".zip", ".bin"]
    )

    def __post_init__(self):
        """Validate and set defaults."""
        # Set dimensions based on model if not explicitly set
        if self.embedding_model in EMBEDDING_DIMENSIONS:
            expected_dim = EMBEDDING_DIMENSIONS[self.embedding_model]
            if self.embedding_dimensions != expected_dim:
                logger.warning(
                    f"Embedding dimensions {self.embedding_dimensions} doesn't match "
                    f"model {self.embedding_model} (expected {expected_dim})"
                )


class EmbeddingManager:
    """Manages text embedding generation using OpenAI API.

    Example:
        ```python
        manager = EmbeddingManager(config=VectorConfig())
        embedding = await manager.embed_text("Hello world")
        embeddings = await manager.embed_texts(["Hello", "World"])
        ```
    """

    def __init__(self, config: Optional[VectorConfig] = None):
        """Initialize embedding manager.

        Args:
            config: Vector configuration (optional)
        """
        self.config = config or VectorConfig()
        self._client = None

    @property
    def client(self):
        """Lazy initialization of OpenAI client."""
        if self._client is None:
            try:
                from openai import AsyncOpenAI

                api_key = self.config.api_key or os.getenv("OPENAI_API_KEY")
                if not api_key:
                    raise ValueError(
                        "OpenAI API key not found. Set OPENAI_API_KEY env var or pass api_key in config."
                    )

                client_kwargs = {"api_key": api_key}
                if self.config.base_url:
                    client_kwargs["base_url"] = self.config.base_url
                elif os.getenv("OPENAI_BASE_URL"):
                    client_kwargs["base_url"] = os.getenv("OPENAI_BASE_URL")

                self._client = AsyncOpenAI(**client_kwargs)
            except ImportError:
                raise ImportError("openai package required. Install with: pip install openai")

        return self._client

    async def embed_text(self, text: str) -> List[float]:
        """Create embedding for a single text.

        Args:
            text: Text to embed

        Returns:
            Embedding vector as list of floats
        """
        embeddings = await self.embed_texts([text])
        return embeddings[0]

    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """Create embeddings for multiple texts.

        Args:
            texts: List of texts to embed

        Returns:
            List of embedding vectors
        """
        if not texts:
            return []

        # Clean texts (replace newlines, strip whitespace)
        cleaned_texts = [t.replace("\n", " ").strip() for t in texts]

        try:
            response = await self.client.embeddings.create(
                input=cleaned_texts,
                model=self.config.embedding_model,
            )

            # Extract embeddings in order
            embeddings = [item.embedding for item in response.data]
            return embeddings

        except Exception as e:
            logger.error(f"Failed to create embeddings: {e}")
            raise

    def chunk_text(self, text: str) -> List[Tuple[int, str]]:
        """Split text into chunks with overlap.

        Args:
            text: Text to chunk

        Returns:
            List of (chunk_index, chunk_text) tuples
        """
        if len(text) <= self.config.chunk_size:
            return [(0, text)]

        chunks = []
        start = 0
        index = 0

        while start < len(text):
            end = start + self.config.chunk_size

            # Try to break at a sentence or word boundary
            if end < len(text):
                # Look for sentence boundary
                for boundary in [". ", ".\n", "? ", "! ", "\n\n"]:
                    boundary_pos = text.rfind(boundary, start + self.config.chunk_size // 2, end)
                    if boundary_pos > start:
                        end = boundary_pos + len(boundary)
                        break
                else:
                    # Fall back to word boundary
                    space_pos = text.rfind(" ", start + self.config.chunk_size // 2, end)
                    if space_pos > start:
                        end = space_pos + 1

            chunk = text[start:end].strip()
            if chunk:
                chunks.append((index, chunk))
                index += 1

            # Move start with overlap
            start = end - self.config.chunk_overlap
            if start >= len(text):
                break

        return chunks


class WorkspaceVectorStore:
    """Vector store for workspace files using PostgreSQL/pgvector.

    Provides storage and retrieval of document embeddings with
    similarity search capabilities.

    Example:
        ```python
        store = WorkspaceVectorStore(
            job_id="abc123",
            connection_string="postgresql://...",
            config=VectorConfig()
        )
        await store.initialize()

        # Index a file
        await store.index_file("notes/research.md", content)

        # Search for similar content
        results = await store.search("GoBD compliance requirements")

        # Cleanup
        await store.cleanup_job()
        await store.close()
        ```
    """

    def __init__(
        self,
        job_id: str,
        connection_string: Optional[str] = None,
        config: Optional[VectorConfig] = None,
        embedding_manager: Optional[EmbeddingManager] = None,
    ):
        """Initialize vector store.

        Args:
            job_id: Job identifier for scoping vectors
            connection_string: PostgreSQL connection string
            config: Vector configuration
            embedding_manager: Pre-configured embedding manager (optional)
        """
        self.job_id = job_id
        self.config = config or VectorConfig()
        self.connection_string = (
            connection_string or self.config.connection_string or os.getenv("DATABASE_URL")
        )

        if not self.connection_string:
            raise ValueError(
                "Database connection string required. Set DATABASE_URL env var or pass connection_string."
            )

        self._embedding_manager = embedding_manager
        self._pool = None
        self._initialized = False

    @property
    def embedding_manager(self) -> EmbeddingManager:
        """Get or create embedding manager."""
        if self._embedding_manager is None:
            self._embedding_manager = EmbeddingManager(self.config)
        return self._embedding_manager

    async def initialize(self) -> None:
        """Initialize database connection pool."""
        if self._initialized:
            return

        try:
            import asyncpg

            self._pool = await asyncpg.create_pool(
                self.connection_string,
                min_size=1,
                max_size=5,
            )
            self._initialized = True
            logger.info(f"WorkspaceVectorStore initialized for job {self.job_id}")

        except ImportError:
            raise ImportError("asyncpg package required. Install with: pip install asyncpg")
        except Exception as e:
            logger.error(f"Failed to initialize vector store: {e}")
            raise

    async def close(self) -> None:
        """Close database connection pool."""
        if self._pool:
            await self._pool.close()
            self._pool = None
            self._initialized = False

    def _compute_hash(self, content: str) -> str:
        """Compute SHA256 hash of content."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _should_index(self, file_path: str, content: str) -> bool:
        """Check if a file should be indexed.

        Args:
            file_path: Path to the file
            content: File content

        Returns:
            True if file should be indexed
        """
        # Check file extension
        for ext in self.config.skip_extensions:
            if file_path.lower().endswith(ext):
                return False

        # Check content length
        if len(content.strip()) < self.config.min_content_length:
            return False

        return True

    async def index_file(
        self,
        file_path: str,
        content: str,
        force_reindex: bool = False,
    ) -> int:
        """Index a file's content.

        Args:
            file_path: Relative path to the file
            content: File content to index
            force_reindex: If True, reindex even if content unchanged

        Returns:
            Number of chunks indexed
        """
        if not self._initialized:
            await self.initialize()

        if not self._should_index(file_path, content):
            logger.debug(f"Skipping indexing for {file_path}")
            return 0

        content_hash = self._compute_hash(content)

        # Check if already indexed with same hash
        if not force_reindex:
            async with self._pool.acquire() as conn:
                existing = await conn.fetchval(
                    """
                    SELECT content_hash FROM workspace_embeddings
                    WHERE job_id = $1 AND file_path = $2 AND chunk_index = 0
                    """,
                    self.job_id,
                    file_path,
                )
                if existing == content_hash:
                    logger.debug(f"File unchanged, skipping: {file_path}")
                    return 0

        # Remove old embeddings for this file
        await self.delete_file(file_path)

        # Chunk the content
        chunks = self.embedding_manager.chunk_text(content)

        if not chunks:
            return 0

        # Generate embeddings
        chunk_texts = [chunk[1] for chunk in chunks]
        try:
            embeddings = await self.embedding_manager.embed_texts(chunk_texts)
        except Exception as e:
            logger.error(f"Failed to generate embeddings for {file_path}: {e}")
            return 0

        # Store embeddings
        async with self._pool.acquire() as conn:
            for (chunk_index, chunk_text), embedding in zip(chunks, embeddings):
                preview = chunk_text[:500] if len(chunk_text) > 500 else chunk_text

                await conn.execute(
                    """
                    INSERT INTO workspace_embeddings
                    (job_id, file_path, chunk_index, content_hash, content_preview,
                     char_count, embedding, embedding_model)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (job_id, file_path, chunk_index)
                    DO UPDATE SET
                        content_hash = EXCLUDED.content_hash,
                        content_preview = EXCLUDED.content_preview,
                        char_count = EXCLUDED.char_count,
                        embedding = EXCLUDED.embedding,
                        embedding_model = EXCLUDED.embedding_model,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    self.job_id,
                    file_path,
                    chunk_index,
                    content_hash,
                    preview,
                    len(chunk_text),
                    str(embedding),  # pgvector accepts string representation
                    self.config.embedding_model,
                )

        logger.debug(f"Indexed {len(chunks)} chunks for {file_path}")
        return len(chunks)

    async def delete_file(self, file_path: str) -> int:
        """Remove embeddings for a file.

        Args:
            file_path: Relative path to the file

        Returns:
            Number of embeddings deleted
        """
        if not self._initialized:
            await self.initialize()

        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM workspace_embeddings
                WHERE job_id = $1 AND file_path = $2
                """,
                self.job_id,
                file_path,
            )
            # Extract count from "DELETE N"
            deleted = int(result.split()[-1]) if result else 0
            if deleted > 0:
                logger.debug(f"Deleted {deleted} embeddings for {file_path}")
            return deleted

    async def search(
        self,
        query: str,
        top_k: Optional[int] = None,
        threshold: Optional[float] = None,
        file_pattern: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Search for similar content in the workspace.

        Args:
            query: Search query text
            top_k: Maximum number of results (default: config.default_top_k)
            threshold: Minimum similarity score (default: config.similarity_threshold)
            file_pattern: Optional SQL LIKE pattern to filter files (e.g., 'notes/%')

        Returns:
            List of search results with file_path, chunk_index, content_preview, similarity
        """
        if not self._initialized:
            await self.initialize()

        top_k = top_k or self.config.default_top_k
        threshold = threshold or self.config.similarity_threshold

        # Generate query embedding
        try:
            query_embedding = await self.embedding_manager.embed_text(query)
        except Exception as e:
            logger.error(f"Failed to embed query: {e}")
            return []

        # Build query
        query_sql = """
            SELECT
                file_path,
                chunk_index,
                content_preview,
                1 - (embedding <=> $1::vector) AS similarity
            FROM workspace_embeddings
            WHERE job_id = $2
              AND 1 - (embedding <=> $1::vector) >= $3
        """
        params = [str(query_embedding), self.job_id, threshold]

        if file_pattern:
            query_sql += " AND file_path LIKE $4"
            params.append(file_pattern)

        query_sql += """
            ORDER BY embedding <=> $1::vector
            LIMIT ${}
        """.format(
            len(params) + 1
        )
        params.append(top_k)

        # Execute search
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query_sql, *params)

        results = [
            {
                "file_path": row["file_path"],
                "chunk_index": row["chunk_index"],
                "content_preview": row["content_preview"],
                "similarity": float(row["similarity"]),
            }
            for row in rows
        ]

        return results

    async def cleanup_job(self) -> int:
        """Remove all embeddings for this job.

        Returns:
            Number of embeddings deleted
        """
        if not self._initialized:
            await self.initialize()

        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM workspace_embeddings WHERE job_id = $1
                """,
                self.job_id,
            )
            deleted = int(result.split()[-1]) if result else 0
            logger.info(f"Cleaned up {deleted} embeddings for job {self.job_id}")
            return deleted

    async def get_stats(self) -> Dict[str, Any]:
        """Get statistics about indexed content.

        Returns:
            Dictionary with file count, chunk count, etc.
        """
        if not self._initialized:
            await self.initialize()

        async with self._pool.acquire() as conn:
            stats = await conn.fetchrow(
                """
                SELECT
                    COUNT(DISTINCT file_path) AS file_count,
                    COUNT(*) AS chunk_count,
                    SUM(char_count) AS total_chars
                FROM workspace_embeddings
                WHERE job_id = $1
                """,
                self.job_id,
            )

        return {
            "job_id": self.job_id,
            "file_count": stats["file_count"] or 0,
            "chunk_count": stats["chunk_count"] or 0,
            "total_chars": stats["total_chars"] or 0,
        }


async def create_workspace_vector_store(
    job_id: str,
    connection_string: Optional[str] = None,
    config: Optional[VectorConfig] = None,
) -> WorkspaceVectorStore:
    """Factory function to create and initialize a vector store.

    Args:
        job_id: Job identifier
        connection_string: PostgreSQL connection string (optional)
        config: Vector configuration (optional)

    Returns:
        Initialized WorkspaceVectorStore instance
    """
    store = WorkspaceVectorStore(
        job_id=job_id,
        connection_string=connection_string,
        config=config,
    )
    await store.initialize()
    return store


class VectorizedWorkspaceManager:
    """Workspace manager with automatic vector indexing.

    Wraps a WorkspaceManager and automatically indexes files
    when they are written. Supports both synchronous and
    asynchronous file operations.

    Example:
        ```python
        workspace = WorkspaceManager(job_id="abc123")
        workspace.initialize()

        vector_store = await create_workspace_vector_store("abc123")

        # Create vectorized wrapper
        vectorized = VectorizedWorkspaceManager(workspace, vector_store)

        # Files are automatically indexed on write
        await vectorized.write_file_async("notes/research.md", content)

        # Search for similar content
        results = await vectorized.semantic_search("GoBD requirements")
        ```
    """

    def __init__(
        self,
        workspace_manager,  # WorkspaceManager
        vector_store: Optional[WorkspaceVectorStore] = None,
        auto_index: bool = True,
    ):
        """Initialize vectorized workspace manager.

        Args:
            workspace_manager: Underlying workspace manager
            vector_store: Optional vector store for semantic search
            auto_index: If True, automatically index files on write
        """
        self._workspace = workspace_manager
        self._vector_store = vector_store
        self._auto_index = auto_index
        self._pending_index_tasks: List[asyncio.Task] = []

    @property
    def workspace(self):
        """Get the underlying workspace manager."""
        return self._workspace

    @property
    def vector_store(self) -> Optional[WorkspaceVectorStore]:
        """Get the vector store."""
        return self._vector_store

    @property
    def job_id(self) -> str:
        """Get the job ID."""
        return self._workspace.job_id

    @property
    def path(self):
        """Get the workspace path."""
        return self._workspace.path

    def set_vector_store(self, store: WorkspaceVectorStore) -> None:
        """Set or replace the vector store.

        Args:
            store: Vector store to use
        """
        self._vector_store = store

    # Delegate standard workspace methods
    def get_path(self, relative_path: str = ""):
        return self._workspace.get_path(relative_path)

    def exists(self, relative_path: str) -> bool:
        return self._workspace.exists(relative_path)

    def read_file(self, relative_path: str) -> str:
        return self._workspace.read_file(relative_path)

    def list_files(self, relative_path: str = "", pattern: str = "*"):
        return self._workspace.list_files(relative_path, pattern)

    def search_files(self, query: str, path: str = "", case_sensitive: bool = False):
        return self._workspace.search_files(query, path, case_sensitive)

    def get_size(self, relative_path: str = "") -> int:
        return self._workspace.get_size(relative_path)

    def get_summary(self) -> dict:
        return self._workspace.get_summary()

    # Write operations with optional indexing
    def write_file(self, relative_path: str, content: str):
        """Write file synchronously (indexing happens in background).

        Args:
            relative_path: Path relative to workspace root
            content: Content to write

        Returns:
            Absolute path to written file
        """
        result = self._workspace.write_file(relative_path, content)

        # Schedule background indexing if enabled
        if self._auto_index and self._vector_store:
            try:
                # Create background task for indexing
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    task = asyncio.create_task(
                        self._index_file_background(relative_path, content)
                    )
                    self._pending_index_tasks.append(task)
            except RuntimeError:
                # No event loop - skip async indexing
                pass

        return result

    async def write_file_async(self, relative_path: str, content: str):
        """Write file and index asynchronously.

        Args:
            relative_path: Path relative to workspace root
            content: Content to write

        Returns:
            Absolute path to written file
        """
        result = self._workspace.write_file(relative_path, content)

        # Index immediately if enabled
        if self._auto_index and self._vector_store:
            await self._vector_store.index_file(relative_path, content)

        return result

    def append_file(self, relative_path: str, content: str):
        """Append to file synchronously.

        Note: Appends don't trigger re-indexing automatically.
        Call index_file() manually if needed.
        """
        return self._workspace.append_file(relative_path, content)

    async def append_file_async(self, relative_path: str, content: str):
        """Append to file and optionally re-index.

        For efficiency, this doesn't automatically re-index.
        Call index_file() manually after completing appends.
        """
        return self._workspace.append_file(relative_path, content)

    def delete_file(self, relative_path: str) -> bool:
        """Delete file synchronously.

        Also removes vector embeddings for the file.
        """
        result = self._workspace.delete_file(relative_path)

        # Remove from vector store
        if result and self._vector_store:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(self._vector_store.delete_file(relative_path))
            except RuntimeError:
                pass

        return result

    async def delete_file_async(self, relative_path: str) -> bool:
        """Delete file and remove vector embeddings.

        Args:
            relative_path: Path relative to workspace root

        Returns:
            True if deleted, False if not found
        """
        result = self._workspace.delete_file(relative_path)

        if result and self._vector_store:
            await self._vector_store.delete_file(relative_path)

        return result

    # Vector-specific methods
    async def index_file(self, relative_path: str, force: bool = False) -> int:
        """Index a file for semantic search.

        Args:
            relative_path: Path to the file
            force: If True, re-index even if content unchanged

        Returns:
            Number of chunks indexed
        """
        if not self._vector_store:
            logger.warning("No vector store configured, skipping indexing")
            return 0

        content = self._workspace.read_file(relative_path)
        return await self._vector_store.index_file(relative_path, content, force_reindex=force)

    async def semantic_search(
        self,
        query: str,
        top_k: int = 10,
        file_pattern: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Search for similar content using semantic similarity.

        Args:
            query: Natural language query
            top_k: Maximum results to return
            file_pattern: Optional SQL LIKE pattern to filter files

        Returns:
            List of search results
        """
        if not self._vector_store:
            logger.warning("No vector store configured")
            return []

        return await self._vector_store.search(
            query=query,
            top_k=top_k,
            file_pattern=file_pattern,
        )

    async def cleanup(self) -> bool:
        """Clean up workspace and vector embeddings.

        Returns:
            True if cleanup succeeded
        """
        # Clean up vector store first
        if self._vector_store:
            await self._vector_store.cleanup_job()

        # Then clean up workspace files
        return self._workspace.cleanup()

    async def close(self) -> None:
        """Close vector store connections."""
        # Wait for any pending indexing tasks
        if self._pending_index_tasks:
            await asyncio.gather(*self._pending_index_tasks, return_exceptions=True)
            self._pending_index_tasks.clear()

        if self._vector_store:
            await self._vector_store.close()

    async def _index_file_background(self, path: str, content: str) -> None:
        """Background task to index a file."""
        try:
            await self._vector_store.index_file(path, content)
        except Exception as e:
            logger.warning(f"Background indexing failed for {path}: {e}")
