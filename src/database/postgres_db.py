"""PostgreSQL Database Manager with async connection pooling.

This module provides a modern async PostgreSQL interface using asyncpg with:
- Async connection pooling
- Namespace-based operations (jobs, requirements, citations)
- Named query loading from SQL files
- CRUD operations with proper async patterns

Part of Phase 1 database refactoring - see docs/db_refactor.md
"""

import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, Any, List, Dict

try:
    import asyncpg
except ImportError:
    asyncpg = None

logger = logging.getLogger(__name__)

QUERIES_DIR = Path(__file__).parent / "queries" / "postgres"


class PostgresDB:
    """PostgreSQL database manager with async connection pooling.

    Provides namespace-based operations for:
    - jobs: Job tracking and management
    - requirements: Requirement storage and queries
    - citations: Citation management

    Example:
        ```python
        db = PostgresDB()
        await db.connect()

        # Create a job
        job_id = await db.jobs.create(
            description="Extract requirements",
            document_path="doc.pdf"
        )

        # Get pending jobs
        jobs = await db.jobs.get_pending()

        await db.close()
        ```
    """

    def __init__(
        self,
        connection_string: Optional[str] = None,
        min_connections: int = None,
        max_connections: int = None,
        command_timeout: float = None,
    ):
        """Initialize PostgreSQL database manager.

        Args:
            connection_string: PostgreSQL connection URL. Falls back to DATABASE_URL env var.
            min_connections: Minimum pool size (default: 2)
            max_connections: Maximum pool size (default: 10)
            command_timeout: Query timeout in seconds (default: 60.0)

        Raises:
            ImportError: If asyncpg is not installed
            ValueError: If no connection string provided
        """
        if asyncpg is None:
            raise ImportError(
                "asyncpg is required for PostgreSQL support. "
                "Install it with: pip install asyncpg"
            )

        from src.utils.db_url import build_postgres_url

        self._connection_string = connection_string or build_postgres_url(
            "POSTGRES",
            fallback_env="DATABASE_URL",
        )
        if not self._connection_string:
            raise ValueError(
                "Database connection string required. Set "
                "POSTGRES_USER + POSTGRES_PASSWORD (with POSTGRES_HOST/PORT/DB "
                "from ConfigMap) or DATABASE_URL, or pass connection_string."
            )

        self._min_connections = min_connections or int(
            os.getenv("POSTGRES_MIN_CONNECTIONS", "2")
        )
        self._max_connections = max_connections or int(
            os.getenv("POSTGRES_MAX_CONNECTIONS", "10")
        )
        self._command_timeout = command_timeout or 60.0

        self._pool: Optional[asyncpg.Pool] = None
        self._queries: Dict[str, str] = {}  # Cache for loaded queries

        # Initialize namespaces
        self.jobs = JobsNamespace(self)
        self.citations = CitationsNamespace(self)

        logger.info("PostgresDB initialized (not connected yet)")

    async def connect(self) -> None:
        """Establish async connection pool.

        Creates an asyncpg connection pool with configured size and timeout.
        Registers pgvector type codec on each connection if available.
        This method is idempotent - safe to call multiple times.
        """
        if self._pool is None:

            async def _init_connection(conn):
                """Register pgvector type codec on new connections."""
                try:
                    from pgvector.asyncpg import register_vector

                    await register_vector(conn)
                except (ImportError, ValueError):
                    pass  # pgvector not installed or extension not on this DB

            self._pool = await asyncpg.create_pool(
                self._connection_string,
                min_size=self._min_connections,
                max_size=self._max_connections,
                command_timeout=self._command_timeout,
                init=_init_connection,
            )
            logger.info(
                f"PostgreSQL connection pool established "
                f"(min={self._min_connections}, max={self._max_connections})"
            )

    async def close(self) -> None:
        """Close connection pool.

        Gracefully closes all connections in the pool.
        This method is idempotent - safe to call multiple times.
        """
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info("PostgreSQL connection pool closed")

    @asynccontextmanager
    async def acquire(self):
        """Acquire a connection from the pool.

        Context manager for getting a connection from the pool.
        Connection is automatically returned when context exits.

        Yields:
            asyncpg.Connection: Database connection

        Raises:
            RuntimeError: If not connected to database
        """
        if self._pool is None:
            raise RuntimeError("Not connected to database. Call connect() first.")
        async with self._pool.acquire() as conn:
            yield conn

    async def execute(self, query: str, *args) -> str:
        """Execute a query without returning results.

        Args:
            query: SQL query string with $1, $2, etc. placeholders
            *args: Query parameters

        Returns:
            Command status string (e.g., "UPDATE 1")
        """
        async with self.acquire() as conn:
            return await conn.execute(query, *args)

    async def fetch(self, query: str, *args) -> List[asyncpg.Record]:
        """Fetch multiple rows.

        Args:
            query: SQL query string with $1, $2, etc. placeholders
            *args: Query parameters

        Returns:
            List of Record objects
        """
        async with self.acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args) -> Optional[asyncpg.Record]:
        """Fetch a single row.

        Args:
            query: SQL query string with $1, $2, etc. placeholders
            *args: Query parameters

        Returns:
            Single Record or None if no results
        """
        async with self.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetchval(self, query: str, *args) -> Any:
        """Fetch a single value.

        Args:
            query: SQL query string with $1, $2, etc. placeholders
            *args: Query parameters

        Returns:
            Single value from first column of first row
        """
        async with self.acquire() as conn:
            return await conn.fetchval(query, *args)

    def _load_query(self, filename: str, query_name: str) -> str:
        """Load a named query from a .sql file.

        Queries are cached after first load. Query files use the format:

        ```sql
        -- name: query_name
        SELECT ...;

        -- name: another_query
        SELECT ...;
        ```

        Args:
            filename: SQL file name (e.g., "complex.sql")
            query_name: Name of the query to load

        Returns:
            SQL query string

        Raises:
            ValueError: If query not found in file
        """
        cache_key = f"{filename}:{query_name}"
        if cache_key in self._queries:
            return self._queries[cache_key]

        file_path = QUERIES_DIR / filename
        if not file_path.exists():
            raise ValueError(f"Query file not found: {file_path}")

        content = file_path.read_text()

        # Parse named queries: -- name: query_name
        pattern = r"--\s*name:\s*(\w+)\s*\n(.*?)(?=--\s*name:|\Z)"
        matches = re.findall(pattern, content, re.DOTALL)

        for name, sql in matches:
            self._queries[f"{filename}:{name}"] = sql.strip()

        if cache_key not in self._queries:
            raise ValueError(f"Query '{query_name}' not found in {filename}")

        return self._queries[cache_key]

    @staticmethod
    def _row_to_dict(row: Optional[asyncpg.Record]) -> Optional[Dict[str, Any]]:
        """Convert asyncpg Record to dictionary.

        Args:
            row: asyncpg Record or None

        Returns:
            Dictionary with column names as keys, or None if row is None
        """
        if row is None:
            return None
        return dict(row)

    @property
    def is_connected(self) -> bool:
        """Check if connected to database."""
        return self._pool is not None

    async def get_thread(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """Get thread by ID."""
        row = await self.fetchrow(
            "SELECT * FROM threads WHERE id = $1",
            thread_id,
        )
        return self._row_to_dict(row)

    async def end_thread(self, thread_id: str) -> None:
        """End a persistent thread."""
        async with self.acquire() as conn:
            await conn.execute(
                """
                UPDATE threads
                SET status   = 'ended',
                    ended_at = CURRENT_TIMESTAMP
                WHERE id = $1
                """,
                thread_id,
            )

    async def update_thread_status(self, thread_id: str, status: str) -> None:
        """Update thread status."""
        async with self.acquire() as conn:
            await conn.execute(
                """
                UPDATE threads
                SET status        = $2,
                    last_activity = CURRENT_TIMESTAMP
                WHERE id = $1
                """,
                thread_id,
                status,
            )

    async def get_thread_messages_history(
        self,
        thread_id: str,
        limit: int = 200,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Load thread message history for session resume. Ordered by created_at ASC."""
        rows = await self.fetch(
            """
            SELECT id, role, content, tool_calls, turn_number, metrics, created_at
            FROM thread_messages
            WHERE thread_id = $1
            ORDER BY created_at ASC
                LIMIT $2
            OFFSET $3
            """,
            thread_id,
            limit,
            offset,
        )
        result = []
        for row in rows:
            msg = {
                "id": str(row["id"]),
                "role": row["role"],
                "content": row["content"],
                "tool_calls": json.loads(row["tool_calls"])
                if row["tool_calls"]
                else None,
                "turn_number": row["turn_number"],
                "metrics": json.loads(row["metrics"]) if row["metrics"] else None,
                "created_at": row["created_at"].isoformat()
                if row["created_at"]
                else None,
            }
            result.append(msg)
        return result

    # =========================================================================
    # SYNC WRAPPERS (for scripts and other sync contexts)
    # =========================================================================

    # Class-level event loop for sync wrappers (shared across all instances)
    _sync_loop = None

    @classmethod
    def _get_sync_loop(cls):
        """Get or create a persistent event loop for sync operations.

        Returns the same event loop across all calls, allowing asyncpg
        connection pools to persist between sync wrapper calls.
        """
        import asyncio

        if cls._sync_loop is None or cls._sync_loop.is_closed():
            cls._sync_loop = asyncio.new_event_loop()
        return cls._sync_loop

    @classmethod
    def _run_async(cls, coro):
        """Helper to run async coroutines in sync context.

        Uses a persistent event loop to execute coroutines, allowing
        asyncpg connection pools to persist between calls.

        Args:
            coro: Async coroutine to execute

        Returns:
            Result of the coroutine
        """
        import asyncio

        try:
            # Check if we're already in an async context
            loop = asyncio.get_running_loop()
            if loop.is_running():
                raise RuntimeError(
                    "Cannot use sync wrapper inside async context. "
                    "Use async methods directly instead."
                )
        except RuntimeError:
            pass  # No running loop, that's fine

        loop = cls._get_sync_loop()
        return loop.run_until_complete(coro)

    def connect_sync(self) -> None:
        """Synchronous wrapper for connect().

        Establishes async connection pool in sync context.
        """
        self._run_async(self.connect())

    def close_sync(self) -> None:
        """Synchronous wrapper for close().

        Closes connection pool in sync context.
        """
        self._run_async(self.close())

    def execute_sync(self, query: str, *args) -> str:
        """Synchronous wrapper for execute().

        Args:
            query: SQL query with $1, $2, etc. placeholders
            *args: Query parameters

        Returns:
            Command status string
        """
        return self._run_async(self.execute(query, *args))

    def fetch_sync(self, query: str, *args) -> List[Dict[str, Any]]:
        """Synchronous wrapper for fetch().

        Args:
            query: SQL query with $1, $2, etc. placeholders
            *args: Query parameters

        Returns:
            List of row dictionaries
        """
        rows = self._run_async(self.fetch(query, *args))
        return [self._row_to_dict(row) for row in rows]

    def fetchrow_sync(self, query: str, *args) -> Optional[Dict[str, Any]]:
        """Synchronous wrapper for fetchrow().

        Args:
            query: SQL query with $1, $2, etc. placeholders
            *args: Query parameters

        Returns:
            Single row dictionary or None
        """
        row = self._run_async(self.fetchrow(query, *args))
        return self._row_to_dict(row)

    def fetchval_sync(self, query: str, *args) -> Any:
        """Synchronous wrapper for fetchval().

        Args:
            query: SQL query with $1, $2, etc. placeholders
            *args: Query parameters

        Returns:
            Single value from first column of first row
        """
        return self._run_async(self.fetchval(query, *args))


class JobsNamespace:
    """Namespace for job-related operations.

    Provides CRUD operations for the jobs table.
    """

    def __init__(self, db: PostgresDB):
        self.db = db

    async def create(
        self,
        description: str,
        document_path: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> uuid.UUID:
        """Create a new job.

        Args:
            description: Job description - what the agent should accomplish
            document_path: Path to document file (optional)
            context: Additional context dictionary (optional)

        Returns:
            UUID of the created job
        """
        job_id = await self.db.fetchval(
            """
            INSERT INTO jobs (description, document_path, context)
            VALUES ($1, $2, $3)
            RETURNING id
            """,
            description,
            document_path,
            json.dumps(context or {}),
        )
        logger.info(f"Created job {job_id}")
        return job_id

    async def get(self, job_id: uuid.UUID) -> Optional[Dict[str, Any]]:
        """Get job by ID.

        Args:
            job_id: Job UUID

        Returns:
            Job details as dictionary or None if not found
        """
        row = await self.db.fetchrow("SELECT * FROM jobs WHERE id = $1", job_id)
        return self.db._row_to_dict(row)

    async def update_status(
        self,
        job_id: uuid.UUID,
        status: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Update job status fields.

        Args:
            job_id: Job UUID
            status: Main job status (optional)
            error_message: Error message if failed (optional)
        """
        updates = []
        values = []
        idx = 1

        if status is not None:
            updates.append(f"status = ${idx}")
            values.append(status)
            idx += 1

        if error_message is not None:
            updates.append(f"error_message = ${idx}")
            values.append(error_message)
            idx += 1

        if not updates:
            return

        updates.append("updated_at = NOW()")
        values.append(job_id)

        query = f"""
            UPDATE jobs
            SET {", ".join(updates)}
            WHERE id = ${idx}
        """

        await self.db.execute(query, *values)
        logger.debug(f"Updated job {job_id} status")

    async def get_pending(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get pending jobs.

        Args:
            limit: Maximum number of jobs to return

        Returns:
            List of job dictionaries
        """
        rows = await self.db.fetch(
            """
            SELECT * FROM jobs
            WHERE status = 'pending'
            ORDER BY created_at ASC
            LIMIT $1
            """,
            limit,
        )
        return [self.db._row_to_dict(row) for row in rows]

    async def list(
        self, status: Optional[str] = None, limit: int = 100, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """List jobs with optional filtering.

        Args:
            status: Filter by status (optional)
            limit: Maximum number of jobs to return
            offset: Number of jobs to skip

        Returns:
            List of job dictionaries
        """
        if status:
            rows = await self.db.fetch(
                """
                SELECT * FROM jobs
                WHERE status = $1
                ORDER BY created_at DESC
                LIMIT $2 OFFSET $3
                """,
                status,
                limit,
                offset,
            )
        else:
            rows = await self.db.fetch(
                """
                SELECT * FROM jobs
                ORDER BY created_at DESC
                LIMIT $1 OFFSET $2
                """,
                limit,
                offset,
            )
        return [self.db._row_to_dict(row) for row in rows]

    async def store_resolved_config(
        self,
        job_id: uuid.UUID,
        resolved_config: Dict[str, Any],
    ) -> bool:
        """Store the fully resolved config snapshot for a job.

        Only writes if resolved_config is currently NULL (first run only).

        Args:
            job_id: Job UUID
            resolved_config: Fully resolved config dict (agent config + prompts + instructions)

        Returns:
            True if the config was stored, False if already set
        """
        result = await self.db.execute(
            "UPDATE jobs SET resolved_config = $1::jsonb WHERE id = $2 AND resolved_config IS NULL",
            json.dumps(resolved_config),
            job_id,
        )
        stored = result == "UPDATE 1"
        if stored:
            logger.debug(f"Stored resolved config for job {job_id}")
        return stored

    async def get_resolved_config(
        self,
        job_id: uuid.UUID,
    ) -> Optional[Dict[str, Any]]:
        """Get the resolved config snapshot for a job.

        Args:
            job_id: Job UUID

        Returns:
            Resolved config dict or None if not set
        """
        row = await self.db.fetchrow(
            "SELECT resolved_config FROM jobs WHERE id = $1",
            job_id,
        )
        if row and row["resolved_config"]:
            rc = row["resolved_config"]
            return rc if isinstance(rc, dict) else json.loads(rc)
        return None

    # Sync wrappers for scripts
    def create_sync(
        self,
        description: str,
        document_path: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> uuid.UUID:
        """Synchronous wrapper for create()."""
        return PostgresDB._run_async(self.create(description, document_path, context))

    def get_sync(self, job_id: uuid.UUID) -> Optional[Dict[str, Any]]:
        """Synchronous wrapper for get()."""
        return PostgresDB._run_async(self.get(job_id))

    def update_status_sync(
        self,
        job_id: uuid.UUID,
        status: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Synchronous wrapper for update_status()."""
        return PostgresDB._run_async(self.update_status(job_id, status, error_message))

    def get_pending_sync(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Synchronous wrapper for get_pending()."""
        return PostgresDB._run_async(self.get_pending(limit))

    def list_sync(
        self, status: Optional[str] = None, limit: int = 100, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Synchronous wrapper for list()."""
        return PostgresDB._run_async(self.list(status, limit, offset))


class CitationsNamespace:
    """Namespace for citation-related operations.

    Provides CRUD operations for citations (if schema includes citations table).
    """

    def __init__(self, db: PostgresDB):
        self.db = db

    async def edit(
        self,
        citation_id: int,
        claim: Optional[str] = None,
        verbatim_quote: Optional[str] = None,
        quote_context: Optional[str] = None,
        relevance_reasoning: Optional[str] = None,
        confidence: Optional[str] = None,
        extraction_method: Optional[str] = None,
        locator: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Edit fields of a citation.

        When content fields (claim, verbatim_quote, quote_context) change,
        verification_status is reset to 'pending' and verification_notes,
        similarity_score, matched_location are cleared.

        Args:
            citation_id: Citation integer ID
            claim: The assertion being supported
            verbatim_quote: Exact quote from source
            quote_context: Context around the quote
            relevance_reasoning: Why this citation is relevant
            confidence: Confidence level (high, medium, low)
            extraction_method: How the citation was extracted
            locator: Location reference (JSON)

        Raises:
            ValueError: If citation not found or no fields provided
        """
        # Guard: check citation exists
        row = await self.db.fetchrow(
            "SELECT id FROM citations WHERE id = $1", citation_id
        )
        if not row:
            raise ValueError(f"Citation {citation_id} not found")

        # Determine if content fields are changing
        content_fields_changed = any(
            v is not None for v in [claim, verbatim_quote, quote_context]
        )

        # Build dynamic UPDATE clause
        updates = []
        values = []
        idx = 1

        field_map = [
            ("claim", claim, lambda v: v),
            ("verbatim_quote", verbatim_quote, lambda v: v),
            ("quote_context", quote_context, lambda v: v),
            ("relevance_reasoning", relevance_reasoning, lambda v: v),
            ("confidence", confidence, lambda v: v),
            ("extraction_method", extraction_method, lambda v: v),
            ("locator", locator, lambda v: json.dumps(v)),
        ]

        for col, val, transform in field_map:
            if val is not None:
                updates.append(f"{col} = ${idx}")
                values.append(transform(val))
                idx += 1

        if not updates:
            raise ValueError("No fields provided to edit")

        # Reset verification fields when content changes
        if content_fields_changed:
            updates.append("verification_status = 'pending'")
            updates.append("verification_notes = NULL")
            updates.append("similarity_score = NULL")
            updates.append("matched_location = NULL")

        values.append(citation_id)

        query = f"""
            UPDATE citations
            SET {", ".join(updates)}
            WHERE id = ${idx}
        """

        await self.db.execute(query, *values)
        logger.debug(f"Edited citation {citation_id}")

    def edit_sync(self, citation_id: int, **kwargs) -> None:
        """Synchronous wrapper for edit()."""
        return PostgresDB._run_async(self.edit(citation_id, **kwargs))


__all__ = ["PostgresDB"]
