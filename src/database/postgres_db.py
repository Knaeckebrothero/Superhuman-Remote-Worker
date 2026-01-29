"""PostgreSQL Database Manager with async connection pooling.

This module provides a modern async PostgreSQL interface using asyncpg with:
- Async connection pooling
- Namespace-based operations (jobs, requirements, citations)
- Named query loading from SQL files
- CRUD operations with proper async patterns

Part of Phase 1 database refactoring - see docs/db_refactor.md
"""

import os
import logging
import re
from pathlib import Path
from typing import Optional, Any, List, Dict
from contextlib import asynccontextmanager
from datetime import datetime
import json
import uuid

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
            prompt="Extract requirements",
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

        self._connection_string = connection_string or os.getenv("DATABASE_URL")
        if not self._connection_string:
            raise ValueError(
                "Database connection string required. "
                "Set DATABASE_URL environment variable or pass connection_string."
            )

        self._min_connections = min_connections or int(os.getenv("POSTGRES_MIN_CONNECTIONS", "2"))
        self._max_connections = max_connections or int(os.getenv("POSTGRES_MAX_CONNECTIONS", "10"))
        self._command_timeout = command_timeout or 60.0

        self._pool: Optional[asyncpg.Pool] = None
        self._queries: Dict[str, str] = {}  # Cache for loaded queries

        # Initialize namespaces
        self.jobs = JobsNamespace(self)
        self.requirements = RequirementsNamespace(self)
        self.citations = CitationsNamespace(self)

        logger.info("PostgresDB initialized (not connected yet)")

    async def connect(self) -> None:
        """Establish async connection pool.

        Creates an asyncpg connection pool with configured size and timeout.
        This method is idempotent - safe to call multiple times.
        """
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self._connection_string,
                min_size=self._min_connections,
                max_size=self._max_connections,
                command_timeout=self._command_timeout
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

    # =========================================================================
    # SYNC WRAPPERS (for dashboard, scripts, and other sync contexts)
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
        prompt: str,
        document_path: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None
    ) -> uuid.UUID:
        """Create a new job.

        Args:
            prompt: User prompt for the job
            document_path: Path to document file (optional)
            context: Additional context dictionary (optional)

        Returns:
            UUID of the created job
        """
        job_id = await self.db.fetchval(
            """
            INSERT INTO jobs (prompt, document_path, context)
            VALUES ($1, $2, $3)
            RETURNING id
            """,
            prompt,
            document_path,
            json.dumps(context or {})
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
        row = await self.db.fetchrow(
            "SELECT * FROM jobs WHERE id = $1",
            job_id
        )
        return self.db._row_to_dict(row)

    async def update_status(
        self,
        job_id: uuid.UUID,
        status: Optional[str] = None,
        creator_status: Optional[str] = None,
        validator_status: Optional[str] = None,
        error_message: Optional[str] = None
    ) -> None:
        """Update job status fields.

        Args:
            job_id: Job UUID
            status: Main job status (optional)
            creator_status: Creator agent status (optional)
            validator_status: Validator agent status (optional)
            error_message: Error message if failed (optional)
        """
        updates = []
        values = []
        idx = 1

        if status is not None:
            updates.append(f"status = ${idx}")
            values.append(status)
            idx += 1

        if creator_status is not None:
            updates.append(f"creator_status = ${idx}")
            values.append(creator_status)
            idx += 1

        if validator_status is not None:
            updates.append(f"validator_status = ${idx}")
            values.append(validator_status)
            idx += 1

        if error_message is not None:
            updates.append(f"error_message = ${idx}")
            values.append(error_message)
            idx += 1

        if not updates:
            return

        updates.append(f"updated_at = NOW()")
        values.append(job_id)

        query = f"""
            UPDATE jobs
            SET {', '.join(updates)}
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
            limit
        )
        return [self.db._row_to_dict(row) for row in rows]

    async def list(
        self,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
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
                status, limit, offset
            )
        else:
            rows = await self.db.fetch(
                """
                SELECT * FROM jobs
                ORDER BY created_at DESC
                LIMIT $1 OFFSET $2
                """,
                limit, offset
            )
        return [self.db._row_to_dict(row) for row in rows]

    # Sync wrappers for dashboard/scripts
    def create_sync(
        self,
        prompt: str,
        document_path: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None
    ) -> uuid.UUID:
        """Synchronous wrapper for create()."""
        return PostgresDB._run_async(
            self.create(prompt, document_path, context)
        )

    def get_sync(self, job_id: uuid.UUID) -> Optional[Dict[str, Any]]:
        """Synchronous wrapper for get()."""
        return PostgresDB._run_async(self.get(job_id))

    def update_status_sync(
        self,
        job_id: uuid.UUID,
        status: Optional[str] = None,
        creator_status: Optional[str] = None,
        validator_status: Optional[str] = None,
        error_message: Optional[str] = None
    ) -> None:
        """Synchronous wrapper for update_status()."""
        return PostgresDB._run_async(
            self.update_status(job_id, status, creator_status, validator_status, error_message)
        )

    def get_pending_sync(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Synchronous wrapper for get_pending()."""
        return PostgresDB._run_async(self.get_pending(limit))

    def list_sync(
        self,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Synchronous wrapper for list()."""
        return PostgresDB._run_async(self.list(status, limit, offset))


class RequirementsNamespace:
    """Namespace for requirement-related operations.

    Provides CRUD operations for the requirements table.
    """

    def __init__(self, db: PostgresDB):
        self.db = db

    async def create(
        self,
        job_id: uuid.UUID,
        text: str,
        name: Optional[str] = None,
        requirement_id: Optional[str] = None,
        req_type: str = "functional",
        priority: str = "medium",
        source_document: Optional[str] = None,
        source_location: Optional[Dict[str, Any]] = None,
        gobd_relevant: bool = False,
        gdpr_relevant: bool = False,
        citations: Optional[List[str]] = None,
        reasoning: Optional[str] = None,
        research_notes: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> uuid.UUID:
        """Create a new requirement.

        Args:
            job_id: Associated job UUID
            text: Requirement text (required)
            name: Short name/title (optional, max 500 chars)
            requirement_id: Optional external/canonical ID (e.g., "R001")
            req_type: Requirement type (functional, compliance, constraint, non_functional)
            priority: Priority level (high, medium, low)
            source_document: Source document path
            source_location: Source location metadata (e.g., {"section": "3.2", "page": 5})
            gobd_relevant: GoBD compliance relevance flag
            gdpr_relevant: GDPR compliance relevance flag
            citations: List of citation IDs
            reasoning: Extraction reasoning/justification
            research_notes: Additional research notes
            tags: Optional tags for categorization

        Returns:
            UUID of created requirement
        """
        req_uuid = await self.db.fetchval(
            """
            INSERT INTO requirements (
                job_id, text, name, requirement_id, type, priority,
                source_document, source_location,
                gobd_relevant, gdpr_relevant,
                citations, reasoning, research_notes, tags,
                status, created_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6,
                $7, $8,
                $9, $10,
                $11, $12, $13, $14,
                'pending', NOW()
            )
            RETURNING id
            """,
            job_id,
            text,
            name[:500] if name else None,  # Enforce max length
            requirement_id,
            req_type,
            priority,
            source_document,
            json.dumps(source_location) if source_location else None,
            gobd_relevant,
            gdpr_relevant,
            json.dumps(citations or []),
            reasoning,
            research_notes,
            json.dumps(tags or []),
        )
        logger.debug(f"Created requirement {requirement_id or req_uuid} (uuid={req_uuid})")
        return req_uuid

    async def get(self, requirement_uuid: uuid.UUID) -> Optional[Dict[str, Any]]:
        """Get requirement by UUID.

        Args:
            requirement_uuid: Requirement UUID

        Returns:
            Requirement details as dictionary or None if not found
        """
        row = await self.db.fetchrow(
            "SELECT * FROM requirements WHERE id = $1",
            requirement_uuid
        )
        return self.db._row_to_dict(row)

    async def get_by_requirement_id(
        self,
        job_id: uuid.UUID,
        requirement_id: str
    ) -> Optional[Dict[str, Any]]:
        """Get requirement by external requirement ID within a job.

        Args:
            job_id: Job UUID
            requirement_id: External requirement ID (e.g., "R001")

        Returns:
            Requirement details as dictionary or None if not found
        """
        row = await self.db.fetchrow(
            """
            SELECT * FROM requirements
            WHERE job_id = $1 AND requirement_id = $2
            """,
            job_id, requirement_id
        )
        return self.db._row_to_dict(row)

    async def list_by_job(
        self,
        job_id: uuid.UUID,
        status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """List requirements for a job.

        Args:
            job_id: Job UUID
            status: Filter by status (optional)

        Returns:
            List of requirement dictionaries
        """
        if status:
            rows = await self.db.fetch(
                """
                SELECT * FROM requirements
                WHERE job_id = $1 AND status = $2
                ORDER BY created_at ASC
                """,
                job_id, status
            )
        else:
            rows = await self.db.fetch(
                """
                SELECT * FROM requirements
                WHERE job_id = $1
                ORDER BY created_at ASC
                """,
                job_id
            )
        return [self.db._row_to_dict(row) for row in rows]

    async def get_pending_for_validation(
        self,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Get requirements pending Neo4j integration.

        Returns requirements where neo4j_id IS NULL, ordered by creation time.
        This is used by the Validator agent to find work.

        Args:
            limit: Maximum number of requirements to return

        Returns:
            List of requirement dictionaries
        """
        rows = await self.db.fetch(
            """
            SELECT * FROM requirements
            WHERE neo4j_id IS NULL
            AND status = 'pending'
            ORDER BY created_at ASC
            LIMIT $1
            """,
            limit
        )
        return [self.db._row_to_dict(row) for row in rows]

    async def update(
        self,
        requirement_uuid: uuid.UUID,
        status: Optional[str] = None,
        neo4j_id: Optional[str] = None,
        validation_result: Optional[Dict[str, Any]] = None,
        rejection_reason: Optional[str] = None,
        last_error: Optional[str] = None,
        retry_count: Optional[int] = None,
        validated_at: bool = False,
    ) -> None:
        """Update requirement fields.

        Args:
            requirement_uuid: Requirement UUID
            status: New status (pending, validating, integrated, rejected, failed)
            neo4j_id: Neo4j element ID after integration
            validation_result: Validation result metadata
            rejection_reason: Reason for rejection if status=rejected
            last_error: Last error message if failed
            retry_count: Number of retry attempts
            validated_at: If True, set validated_at to NOW()
        """
        updates = []
        values = []
        idx = 1

        if status is not None:
            updates.append(f"status = ${idx}")
            values.append(status)
            idx += 1

        if neo4j_id is not None:
            updates.append(f"neo4j_id = ${idx}")
            values.append(neo4j_id)
            idx += 1

        if validation_result is not None:
            updates.append(f"validation_result = ${idx}")
            values.append(json.dumps(validation_result))
            idx += 1

        if rejection_reason is not None:
            updates.append(f"rejection_reason = ${idx}")
            values.append(rejection_reason)
            idx += 1

        if last_error is not None:
            updates.append(f"last_error = ${idx}")
            values.append(last_error)
            idx += 1

        if retry_count is not None:
            updates.append(f"retry_count = ${idx}")
            values.append(retry_count)
            idx += 1

        if validated_at:
            updates.append("validated_at = NOW()")

        if not updates:
            return

        updates.append("updated_at = NOW()")
        values.append(requirement_uuid)

        query = f"""
            UPDATE requirements
            SET {', '.join(updates)}
            WHERE id = ${idx}
        """

        await self.db.execute(query, *values)
        logger.debug(f"Updated requirement {requirement_uuid}")

    async def update_neo4j_id(
        self,
        requirement_uuid: uuid.UUID,
        neo4j_id: str,
        status: str = "integrated"
    ) -> None:
        """Update requirement with Neo4j element ID.

        Called by Validator after integrating into knowledge graph.
        This is a convenience wrapper around update().

        Args:
            requirement_uuid: Requirement UUID
            neo4j_id: Neo4j element ID
            status: New status (default: "integrated")
        """
        await self.update(
            requirement_uuid=requirement_uuid,
            neo4j_id=neo4j_id,
            status=status,
            validated_at=True
        )
        logger.debug(f"Updated requirement {requirement_uuid} with neo4j_id={neo4j_id}")

    async def edit_content(
        self,
        requirement_uuid: uuid.UUID,
        text: Optional[str] = None,
        name: Optional[str] = None,
        req_type: Optional[str] = None,
        priority: Optional[str] = None,
        source_document: Optional[str] = None,
        source_location: Optional[Dict[str, Any]] = None,
        gobd_relevant: Optional[bool] = None,
        gdpr_relevant: Optional[bool] = None,
        citations: Optional[List[str]] = None,
        reasoning: Optional[str] = None,
        research_notes: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> None:
        """Edit content fields of a pending requirement.

        Only editable when status='pending' (not yet picked up by validator).
        Protected fields (id, job_id, status, neo4j_id, etc.) cannot be set.

        Args:
            requirement_uuid: Requirement UUID
            text: Full requirement text
            name: Short name/title
            req_type: Type (functional, compliance, constraint, non_functional)
            priority: Priority (high, medium, low)
            source_document: Source document path
            source_location: Location in document
            gobd_relevant: GoBD relevance flag
            gdpr_relevant: GDPR relevance flag
            citations: Citation IDs list
            reasoning: Extraction reasoning
            research_notes: Research notes
            tags: Tags list

        Raises:
            ValueError: If requirement not found, not pending, or no fields provided
        """
        # Guard: check current status
        row = await self.db.fetchrow(
            "SELECT status FROM requirements WHERE id = $1",
            requirement_uuid
        )
        if not row:
            raise ValueError(f"Requirement {requirement_uuid} not found")
        if row["status"] != "pending":
            raise ValueError(
                f"Requirement {requirement_uuid} has status '{row['status']}', "
                f"only 'pending' requirements can be edited"
            )

        # Build dynamic UPDATE clause
        updates = []
        values = []
        idx = 1

        field_map = [
            ("text", text, lambda v: v),
            ("name", name, lambda v: v[:500] if v else v),
            ("type", req_type, lambda v: v),
            ("priority", priority, lambda v: v),
            ("source_document", source_document, lambda v: v),
            ("source_location", source_location, lambda v: json.dumps(v)),
            ("gobd_relevant", gobd_relevant, lambda v: v),
            ("gdpr_relevant", gdpr_relevant, lambda v: v),
            ("citations", citations, lambda v: json.dumps(v)),
            ("reasoning", reasoning, lambda v: v),
            ("research_notes", research_notes, lambda v: v),
            ("tags", tags, lambda v: json.dumps(v)),
        ]

        for col, val, transform in field_map:
            if val is not None:
                updates.append(f"{col} = ${idx}")
                values.append(transform(val))
                idx += 1

        if not updates:
            raise ValueError("No fields provided to edit")

        updates.append("updated_at = NOW()")
        values.append(requirement_uuid)

        query = f"""
            UPDATE requirements
            SET {', '.join(updates)}
            WHERE id = ${idx}
        """

        await self.db.execute(query, *values)
        logger.debug(f"Edited content of requirement {requirement_uuid}")

    def edit_content_sync(
        self,
        requirement_uuid: uuid.UUID,
        **kwargs
    ) -> None:
        """Synchronous wrapper for edit_content()."""
        return PostgresDB._run_async(self.edit_content(requirement_uuid, **kwargs))

    # Sync wrappers for dashboard/scripts
    def create_sync(
        self,
        job_id: uuid.UUID,
        text: str,
        name: Optional[str] = None,
        requirement_id: Optional[str] = None,
        req_type: str = "functional",
        priority: str = "medium",
        source_document: Optional[str] = None,
        source_location: Optional[Dict[str, Any]] = None,
        gobd_relevant: bool = False,
        gdpr_relevant: bool = False,
        citations: Optional[List[str]] = None,
        reasoning: Optional[str] = None,
        research_notes: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> uuid.UUID:
        """Synchronous wrapper for create()."""
        return PostgresDB._run_async(
            self.create(
                job_id, text, name, requirement_id, req_type, priority,
                source_document, source_location, gobd_relevant, gdpr_relevant,
                citations, reasoning, research_notes, tags
            )
        )

    def get_sync(self, requirement_uuid: uuid.UUID) -> Optional[Dict[str, Any]]:
        """Synchronous wrapper for get()."""
        return PostgresDB._run_async(self.get(requirement_uuid))

    def list_by_job_sync(
        self,
        job_id: uuid.UUID,
        status: Optional[str] = None,
        limit: int = 1000
    ) -> List[Dict[str, Any]]:
        """Synchronous wrapper for list_by_job()."""
        return PostgresDB._run_async(self.list_by_job(job_id, status, limit))

    def get_pending_for_validation_sync(
        self,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Synchronous wrapper for get_pending_for_validation()."""
        return PostgresDB._run_async(self.get_pending_for_validation(limit))

    def update_sync(
        self,
        requirement_uuid: uuid.UUID,
        **kwargs
    ) -> None:
        """Synchronous wrapper for update()."""
        return PostgresDB._run_async(self.update(requirement_uuid, **kwargs))

    def update_neo4j_id_sync(
        self,
        requirement_uuid: uuid.UUID,
        neo4j_id: str,
        status: str = "integrated"
    ) -> None:
        """Synchronous wrapper for update_neo4j_id()."""
        return PostgresDB._run_async(
            self.update_neo4j_id(requirement_uuid, neo4j_id, status)
        )


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
            "SELECT id FROM citations WHERE id = $1",
            citation_id
        )
        if not row:
            raise ValueError(f"Citation {citation_id} not found")

        # Determine if content fields are changing
        content_fields_changed = any(v is not None for v in [claim, verbatim_quote, quote_context])

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
            SET {', '.join(updates)}
            WHERE id = ${idx}
        """

        await self.db.execute(query, *values)
        logger.debug(f"Edited citation {citation_id}")

    def edit_sync(self, citation_id: int, **kwargs) -> None:
        """Synchronous wrapper for edit()."""
        return PostgresDB._run_async(self.edit(citation_id, **kwargs))


__all__ = ['PostgresDB']
