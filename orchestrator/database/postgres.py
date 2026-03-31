"""PostgreSQL Database Manager with async connection pooling.

This module provides the canonical async PostgreSQL interface using asyncpg with:
- Async connection pooling
- Query execution methods (execute, fetch, fetchrow, fetchval)
- Named query loading from SQL files
- Job, agent, and requirement management
- Sync wrappers for non-async contexts

This is the canonical database layer for the orchestrator.
"""

import json
import logging
import math
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Any, List, Dict
from uuid import UUID

try:
    import asyncpg
except ImportError:
    asyncpg = None

logger = logging.getLogger(__name__)

QUERIES_DIR = Path(__file__).parent / "queries" / "postgres"

# Schema file for database initialization
SCHEMA_FILE = Path(__file__).parent / "schema.sql"

# Tables exposed to the cockpit
ALLOWED_TABLES = frozenset(
    {
        "jobs",
        "agents",
        "datasources",
        "users",
        "projects",
        "project_members",
        "project_repositories",
    }
)

# Required tables that must exist for the orchestrator to function
REQUIRED_TABLES = [
    "users",
    "sessions",
    "projects",
    "project_members",
    "project_repositories",
    "jobs",
    "agents",
    "datasources",
    "builder_sessions",
    "builder_messages",
    "user_api_keys",
    "project_api_keys",
]

# Tables in the vector DB (verified separately when VECTOR_DB_URL is set)
VECTOR_REQUIRED_TABLES = [
    "memories",
    "knowledge_index",
    "sources",
    "citations",
    "job_sources",
    "source_annotations",
    "source_tags",
    "source_embeddings",
    "schema_migrations",
]

# Column type mapping from PostgreSQL types to frontend-friendly types
PG_TYPE_MAP = {
    "uuid": "string",
    "text": "string",
    "varchar": "string",
    "character varying": "string",
    "integer": "number",
    "bigint": "number",
    "smallint": "number",
    "real": "number",
    "double precision": "number",
    "numeric": "number",
    "serial": "number",
    "boolean": "boolean",
    "timestamp with time zone": "date",
    "timestamp without time zone": "date",
    "timestamp": "date",
    "date": "date",
    "jsonb": "json",
    "json": "json",
    "bytea": "binary",
}


class PostgresDB:
    """PostgreSQL database manager with async connection pooling.

    Provides core database operations:
    - Connection pool management (connect, close, acquire)
    - Query execution (execute, fetch, fetchrow, fetchval)
    - Named query loading from SQL files
    - Job management (CRUD, status updates, progress tracking)
    - Agent management (registration, heartbeat, status)
    - Requirement queries and statistics
    - Sync wrappers for scripts and other sync contexts

    Example:
        ```python
        db = PostgresDB()
        await db.connect()

        # Execute queries directly
        rows = await db.fetch("SELECT * FROM jobs WHERE status = $1", "pending")

        # Job operations
        job = await db.create_job(description="Extract requirements")
        jobs = await db.get_jobs(status="processing")

        # Agent operations
        result = await db.register_agent(config_name="creator", pod_ip="10.0.0.1")
        await db.heartbeat(agent_id, status="working", current_job_id=job_id)

        # Use sync wrappers in non-async contexts
        db.connect_sync()
        rows = db.fetch_sync("SELECT * FROM jobs LIMIT 10")

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

        self._connection_string = connection_string or os.getenv(
            "DATABASE_URL",
            "postgresql://srw:srw_password@localhost:5432/srw",
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

    # Alias for compatibility
    async def disconnect(self) -> None:
        """Close connection pool (alias for close())."""
        await self.close()

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
    # TABLE OPERATIONS
    # =========================================================================

    async def get_tables(self) -> List[Dict[str, Any]]:
        """Get list of allowed tables with row counts."""
        tables = []
        async with self.acquire() as conn:
            for table in sorted(ALLOWED_TABLES):
                row = await conn.fetchrow(
                    f"SELECT COUNT(*) as count FROM {table}"  # noqa: S608
                )
                tables.append({"name": table, "rowCount": row["count"] if row else 0})
        return tables

    async def get_table_schema(self, table_name: str) -> List[Dict[str, Any]]:
        """Get column definitions for a table."""
        if table_name not in ALLOWED_TABLES:
            raise ValueError(f"Table '{table_name}' not allowed")

        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    column_name,
                    data_type,
                    is_nullable
                FROM information_schema.columns
                WHERE table_name = $1 AND table_schema = 'public'
                ORDER BY ordinal_position
                """,
                table_name,
            )

        return [
            {
                "name": row["column_name"],
                "type": PG_TYPE_MAP.get(row["data_type"], "string"),
                "nullable": row["is_nullable"] == "YES",
            }
            for row in rows
        ]

    async def get_table_data(
        self,
        table_name: str,
        page: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        """Get paginated data from a table.

        Args:
            table_name: Name of table to query
            page: Page number (1-indexed). Use -1 to request the last page.
            page_size: Number of rows per page
        """
        if table_name not in ALLOWED_TABLES:
            raise ValueError(f"Table '{table_name}' not allowed")

        async with self.acquire() as conn:
            # Get total count
            count_row = await conn.fetchrow(
                f"SELECT COUNT(*) as total FROM {table_name}"  # noqa: S608
            )
            total = count_row["total"] if count_row else 0

            # Handle last page request (page=-1)
            if page == -1:
                page = max(1, math.ceil(total / page_size))

            offset = (page - 1) * page_size

            # Get schema for type info
            columns = await self.get_table_schema(table_name)

            # Get data with ordering by created_at/registered_at if available, else by id
            if table_name in ("jobs", "citations", "datasources"):
                order_col = "created_at"
            elif table_name == "agents":
                order_col = "registered_at"
            else:
                order_col = "id"
            rows = await conn.fetch(
                f"SELECT * FROM {table_name} ORDER BY {order_col} DESC LIMIT $1 OFFSET $2",  # noqa: S608
                page_size,
                offset,
            )

            # Convert records to dicts, handling special types
            data = []
            for row in rows:
                row_dict = {}
                for key, value in dict(row).items():
                    if isinstance(value, bytes):
                        row_dict[key] = f"<binary: {len(value)} bytes>"
                    else:
                        row_dict[key] = value
                data.append(row_dict)

        return {
            "columns": columns,
            "rows": data,
            "total": total,
            "page": page,
            "pageSize": page_size,
        }

    # =========================================================================
    # JOB OPERATIONS
    # =========================================================================

    async def get_jobs(
        self,
        status: str | None = None,
        user_id: str | None = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Get list of jobs with optional status and user filter.

        Args:
            status: Optional status filter (e.g., 'completed', 'processing')
            user_id: Optional user ID filter
            limit: Maximum number of jobs to return

        Returns:
            List of job dicts with id, description, status, config_name, created_at, user_id
        """
        conditions = []
        values = []
        param_count = 0

        if status:
            param_count += 1
            conditions.append(f"status = ${param_count}")
            values.append(status)

        if user_id:
            param_count += 1
            conditions.append(f"user_id = ${param_count}")
            values.append(UUID(user_id))

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        param_count += 1
        values.append(limit)

        async with self.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, description, status,
                       config_name, assigned_agent_id, user_id,
                       project_id, parent_job_id, priority,
                       repo_name, branch_name, merge_status, created_at,
                       context->'snapshot'->>'status' AS snapshot_status
                FROM jobs
                {where_clause}
                ORDER BY created_at DESC
                LIMIT ${param_count}
                """,
                *values,
            )

        return [dict(row) for row in rows]

    async def get_job(self, job_id: str) -> Dict[str, Any] | None:
        """Get a single job by ID.

        Args:
            job_id: The job UUID as string

        Returns:
            Job dict or None if not found
        """
        try:
            uuid_val = UUID(job_id)
        except ValueError:
            return None

        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, status,
                       config_name, config_override, resolved_config,
                       assigned_agent_id, user_id,
                       project_id, parent_job_id, priority,
                       branch_name, repo_name, merge_status, repo_merge_statuses,
                       freeze_data,
                       creation_order, worktree_path, delegation_context,
                       created_at, updated_at, description, context
                FROM jobs
                WHERE id = $1
                """,
                uuid_val,
            )

        return dict(row) if row else None

    async def create_job(
        self,
        description: str,
        document_path: str | None = None,
        document_dir: str | None = None,
        config_name: str = "default",
        config_override: Dict[str, Any] | None = None,
        context: Dict[str, Any] | None = None,
        user_id: str | None = None,
        project_id: str | None = None,
        branch_name: str | None = None,
        parent_job_id: str | None = None,
        priority: int = 5,
        repo_name: str | None = None,
        creation_order: int | None = None,
        worktree_path: str | None = None,
        delegation_context: str | None = None,
    ) -> Dict[str, Any]:
        """Create a new job.

        Args:
            description: Job description - what the agent should accomplish
            document_path: Optional path to a document
            document_dir: Optional directory containing documents
            config_name: Agent configuration name (default: "default")
            config_override: Optional per-job configuration overrides
            context: Optional context dictionary
            user_id: Optional user UUID who created this job
            project_id: Optional project UUID this job belongs to
            branch_name: Optional git branch name for this job
            parent_job_id: Optional parent job UUID (for verification/follow-up jobs)
            priority: Job priority (0=low, 5=normal, 10=high). Default: 5
            repo_name: Optional Gitea repo name (e.g. "job-ec38de5d")
            creation_order: Optional 0-based index for delegation subagent merge ordering
            worktree_path: Optional git worktree path for delegation subagents
            delegation_context: Optional shared context string from parent delegation

        Returns:
            Created job dict with id
        """
        user_uuid = UUID(user_id) if user_id else None
        project_uuid = UUID(project_id) if project_id else None
        parent_uuid = UUID(parent_job_id) if parent_job_id else None

        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO jobs (description, document_path, config_name, config_override, context, status, user_id, project_id, branch_name, parent_job_id, priority, repo_name, creation_order, worktree_path, delegation_context)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                RETURNING id, status, config_name, assigned_agent_id, user_id, project_id, parent_job_id, priority, branch_name, repo_name, created_at, updated_at, description, creation_order, worktree_path
                """,
                description,
                document_path or document_dir,
                config_name,
                json.dumps(config_override) if config_override else None,
                json.dumps(context) if context else None,
                "created",
                user_uuid,
                project_uuid,
                branch_name,
                parent_uuid,
                priority,
                repo_name,
                creation_order,
                worktree_path,
                delegation_context,
            )

        return dict(row)

    async def delete_job(self, job_id: str) -> bool:
        """Delete a job (cascades to requirements).

        Args:
            job_id: Job UUID as string

        Returns:
            True if deleted, False if not found
        """
        try:
            uuid_val = UUID(job_id)
        except ValueError:
            return False

        async with self.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM jobs WHERE id = $1",
                uuid_val,
            )

        return result == "DELETE 1"

    async def cancel_job(self, job_id: str) -> bool:
        """Cancel a job by setting its status to 'cancelled'.

        Clears assigned_agent_id so the agent is no longer associated with this job.

        Args:
            job_id: Job UUID as string

        Returns:
            True if cancelled, False if not found or already completed/cancelled
        """
        try:
            uuid_val = UUID(job_id)
        except ValueError:
            return False

        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE jobs
                SET status = 'cancelled',
                    assigned_agent_id = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = $1 AND status NOT IN ('completed', 'cancelled')
                """,
                uuid_val,
            )

        return result == "UPDATE 1"

    async def pause_job(self, job_id: str) -> bool:
        """Pause a running job. Clears assigned_agent_id so the agent is freed.

        The job enters 'paused' status and will be auto-resumed by the dispatcher
        when an agent becomes available.

        Args:
            job_id: Job UUID as string

        Returns:
            True if paused, False if not found or not in a pausable state
        """
        try:
            uuid_val = UUID(job_id)
        except ValueError:
            return False

        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE jobs
                SET status = 'paused',
                    assigned_agent_id = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = $1 AND status = 'processing'
                """,
                uuid_val,
            )

        return result == "UPDATE 1"

    async def update_job_status(
        self,
        job_id: str,
        status: str | None = None,
        assigned_agent_id: str | None = None,
        error_message: str | None = None,
        freeze_data: Dict[str, Any] | None = None,
    ) -> bool:
        """Update job status fields.

        Args:
            job_id: Job UUID as string
            status: New job status
            assigned_agent_id: Agent ID if being assigned
            error_message: Error message if failed
            freeze_data: Freeze metadata dict (for waiting_for_reply, pending_review)

        Returns:
            True if updated, False if not found
        """
        try:
            uuid_val = UUID(job_id)
        except ValueError:
            return False

        # Build dynamic update query
        updates = []
        values = []
        param_count = 0

        if status is not None:
            param_count += 1
            updates.append(f"status = ${param_count}")
            values.append(status)

        if error_message is not None:
            param_count += 1
            updates.append(f"error_message = ${param_count}")
            values.append(error_message)

        if assigned_agent_id is not None:
            param_count += 1
            updates.append(f"assigned_agent_id = ${param_count}")
            values.append(UUID(assigned_agent_id) if assigned_agent_id else None)

        if freeze_data is not None:
            param_count += 1
            updates.append(f"freeze_data = ${param_count}::jsonb")
            values.append(json.dumps(freeze_data))

        if not updates:
            return False

        updates.append("updated_at = CURRENT_TIMESTAMP")
        param_count += 1
        values.append(uuid_val)

        query = f"UPDATE jobs SET {', '.join(updates)} WHERE id = ${param_count}"

        async with self.acquire() as conn:
            result = await conn.execute(query, *values)

        return result == "UPDATE 1"

    async def update_job_merge_status(
        self,
        job_id: str,
        merge_status: str | None = None,
        repo_merge_statuses: dict | None = None,
    ) -> bool:
        """Update merge-related columns on a job.

        Args:
            job_id: Job UUID as string
            merge_status: New merge status (e.g. merged, conflict, skipped)
            repo_merge_statuses: Per-source-repo merge status JSONB dict

        Returns:
            True if updated, False if not found
        """
        try:
            uuid_val = UUID(job_id)
        except ValueError:
            return False

        updates = []
        values = []
        param_count = 0

        if merge_status is not None:
            param_count += 1
            updates.append(f"merge_status = ${param_count}")
            values.append(merge_status)

        if repo_merge_statuses is not None:
            param_count += 1
            updates.append(f"repo_merge_statuses = ${param_count}")
            values.append(json.dumps(repo_merge_statuses))

        if not updates:
            return False

        updates.append("updated_at = CURRENT_TIMESTAMP")
        param_count += 1
        values.append(uuid_val)

        query = f"UPDATE jobs SET {', '.join(updates)} WHERE id = ${param_count}"

        async with self.acquire() as conn:
            result = await conn.execute(query, *values)

        return result == "UPDATE 1"

    async def get_delegation_children(self, parent_job_id: str) -> list[Dict[str, Any]]:
        """Get delegation child jobs ordered by creation_order.

        Delegation children are distinguished from critic/scholar subjobs
        by having a non-NULL creation_order.

        Args:
            parent_job_id: Parent job UUID as string

        Returns:
            List of child job dicts, ordered by creation_order ASC
        """
        try:
            uuid_val = UUID(parent_job_id)
        except ValueError:
            return []

        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM jobs
                WHERE parent_job_id = $1 AND creation_order IS NOT NULL
                ORDER BY creation_order ASC
                """,
                uuid_val,
            )

        return [dict(row) for row in rows]

    async def all_delegation_children_terminal(self, parent_job_id: str) -> bool:
        """Check if all delegation children have reached a terminal status.

        Returns True only if there is at least one delegation child AND all
        of them are in a terminal state (completed, failed, cancelled, or
        pending_review).

        Args:
            parent_job_id: Parent job UUID as string

        Returns:
            True if all delegation children are terminal, False otherwise
        """
        try:
            uuid_val = UUID(parent_job_id)
        except ValueError:
            return False

        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) as total,
                    COUNT(*) FILTER (
                        WHERE status IN ('completed', 'failed', 'cancelled', 'pending_review')
                    ) as terminal
                FROM jobs
                WHERE parent_job_id = $1 AND creation_order IS NOT NULL
                """,
                uuid_val,
            )

        if not row:
            return False
        return row["total"] > 0 and row["total"] == row["terminal"]

    async def get_delegation_depth(self, job_id: str) -> int:
        """Compute the delegation depth of a job.

        Walks the parent_job_id chain upward and counts only delegation
        links (jobs with creation_order IS NOT NULL).  Lifecycle links
        (scholar/critic with creation_order IS NULL) contribute zero depth.

        This implements "Option C": critics and scholars can delegate
        because they don't increase the depth counter.

        Args:
            job_id: Job UUID as string

        Returns:
            Number of delegation links in the ancestor chain (0 for root
            jobs and lifecycle-only subjobs like scholar/critic)
        """
        try:
            uuid_val = UUID(job_id)
        except ValueError:
            return 0

        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                WITH RECURSIVE ancestors AS (
                    SELECT id, parent_job_id, creation_order, 0 AS walk_depth
                    FROM jobs
                    WHERE id = $1

                    UNION ALL

                    SELECT j.id, j.parent_job_id, j.creation_order, a.walk_depth + 1
                    FROM jobs j
                    JOIN ancestors a ON j.id = a.parent_job_id
                    WHERE a.walk_depth < 20
                )
                SELECT COUNT(*) FILTER (WHERE creation_order IS NOT NULL)
                    AS delegation_depth
                FROM ancestors
                """,
                uuid_val,
            )

        return int(row["delegation_depth"]) if row else 0

    async def update_job_context(self, job_id: str, context: Dict[str, Any]) -> bool:
        """Update the context JSONB column for a job.

        Args:
            job_id: Job UUID as string
            context: New context dictionary

        Returns:
            True if updated, False if not found
        """
        import json as json_module

        try:
            uuid_val = UUID(job_id)
        except ValueError:
            return False

        query = (
            "UPDATE jobs SET context = $1, updated_at = CURRENT_TIMESTAMP WHERE id = $2"
        )
        async with self.acquire() as conn:
            result = await conn.execute(query, json_module.dumps(context), uuid_val)

        return result == "UPDATE 1"

    async def merge_job_context(self, job_id: str, updates: Dict[str, Any]) -> bool:
        """Atomically merge updates into the job's context JSONB column.

        Uses PostgreSQL's || operator for a top-level merge, avoiding the
        read-modify-write race in update_job_context().

        Args:
            job_id: Job UUID as string
            updates: Dictionary of keys to merge into existing context

        Returns:
            True if updated, False if not found
        """
        import json as json_module

        try:
            uuid_val = UUID(job_id)
        except ValueError:
            return False

        query = (
            "UPDATE jobs "
            "SET context = COALESCE(context, '{}'::jsonb) || $1::jsonb, "
            "    updated_at = CURRENT_TIMESTAMP "
            "WHERE id = $2"
        )
        async with self.acquire() as conn:
            result = await conn.execute(query, json_module.dumps(updates), uuid_val)

        return result == "UPDATE 1"

    async def merge_vm_context(self, job_id: str, vm_updates: Dict[str, Any]) -> bool:
        """Atomically merge updates into context.vm without touching other keys.

        Uses jsonb_set + || to merge into the nested 'vm' key in a single
        atomic SQL statement, eliminating the read-modify-write race.

        Args:
            job_id: Job UUID as string
            vm_updates: Dictionary of keys to merge into context.vm

        Returns:
            True if updated, False if not found
        """
        import json as json_module

        try:
            uuid_val = UUID(job_id)
        except ValueError:
            return False

        query = (
            "UPDATE jobs "
            "SET context = jsonb_set("
            "    COALESCE(context, '{}'::jsonb), "
            "    '{vm}', "
            "    COALESCE(context->'vm', '{}'::jsonb) || $1::jsonb"
            "), "
            "    updated_at = CURRENT_TIMESTAMP "
            "WHERE id = $2"
        )
        async with self.acquire() as conn:
            result = await conn.execute(query, json_module.dumps(vm_updates), uuid_val)

        return result == "UPDATE 1"

    async def merge_snapshot_context(
        self, job_id: str, snapshot_updates: Dict[str, Any]
    ) -> bool:
        """Atomically merge updates into context.snapshot without touching other keys.

        Uses jsonb_set + || to merge into the nested 'snapshot' key in a single
        atomic SQL statement, same pattern as merge_vm_context().

        Args:
            job_id: Job UUID as string
            snapshot_updates: Dictionary of keys to merge into context.snapshot

        Returns:
            True if updated, False if not found
        """
        import json as json_module

        try:
            uuid_val = UUID(job_id)
        except ValueError:
            return False

        query = (
            "UPDATE jobs "
            "SET context = jsonb_set("
            "    COALESCE(context, '{}'::jsonb), "
            "    '{snapshot}', "
            "    COALESCE(context->'snapshot', '{}'::jsonb) || $1::jsonb"
            "), "
            "    updated_at = CURRENT_TIMESTAMP "
            "WHERE id = $2"
        )
        async with self.acquire() as conn:
            result = await conn.execute(
                query, json_module.dumps(snapshot_updates), uuid_val
            )

        return result == "UPDATE 1"

    async def merge_ide_session_context(
        self, job_id: str, session_updates: Dict[str, Any]
    ) -> bool:
        """Atomically merge updates into context.ide_session without touching other keys.

        Uses jsonb_set + || to merge into the nested 'ide_session' key in a single
        atomic SQL statement, same pattern as merge_vm_context().

        Args:
            job_id: Job UUID as string
            session_updates: Dictionary of keys to merge into context.ide_session

        Returns:
            True if updated, False if not found
        """
        import json as json_module

        try:
            uuid_val = UUID(job_id)
        except ValueError:
            return False

        query = (
            "UPDATE jobs "
            "SET context = jsonb_set("
            "    COALESCE(context, '{}'::jsonb), "
            "    '{ide_session}', "
            "    COALESCE(context->'ide_session', '{}'::jsonb) || $1::jsonb"
            "), "
            "    updated_at = CURRENT_TIMESTAMP "
            "WHERE id = $2"
        )
        async with self.acquire() as conn:
            result = await conn.execute(
                query, json_module.dumps(session_updates), uuid_val
            )

        return result == "UPDATE 1"

    async def merge_workspace_container_context(
        self, job_id: str, container_updates: Dict[str, Any]
    ) -> bool:
        """Atomically merge updates into context.workspace_container.

        Uses jsonb_set + || to merge into the nested 'workspace_container' key
        in a single atomic SQL statement, same pattern as merge_vm_context().

        Args:
            job_id: Job UUID as string
            container_updates: Dictionary of keys to merge into context.workspace_container

        Returns:
            True if updated, False if not found
        """
        import json as json_module

        try:
            uuid_val = UUID(job_id)
        except ValueError:
            return False

        query = (
            "UPDATE jobs "
            "SET context = jsonb_set("
            "    COALESCE(context, '{}'::jsonb), "
            "    '{workspace_container}', "
            "    COALESCE(context->'workspace_container', '{}'::jsonb) || $1::jsonb"
            "), "
            "    updated_at = CURRENT_TIMESTAMP "
            "WHERE id = $2"
        )
        async with self.acquire() as conn:
            result = await conn.execute(
                query, json_module.dumps(container_updates), uuid_val
            )

        return result == "UPDATE 1"

    async def merge_thread_workspace_context(
        self, thread_id: str, container_updates: Dict[str, Any]
    ) -> bool:
        """Atomically merge updates into threads.metadata.workspace_container.

        Same pattern as merge_workspace_container_context() but targets the
        threads table metadata JSONB instead of jobs.context.

        Args:
            thread_id: Thread UUID as string
            container_updates: Dictionary to merge into metadata.workspace_container

        Returns:
            True if updated, False if not found
        """
        import json as json_module

        try:
            uuid_val = UUID(thread_id)
        except ValueError:
            return False

        query = (
            "UPDATE threads "
            "SET metadata = jsonb_set("
            "    COALESCE(metadata, '{}'::jsonb), "
            "    '{workspace_container}', "
            "    COALESCE(metadata->'workspace_container', '{}'::jsonb) || $1::jsonb"
            "), "
            "    last_activity = CURRENT_TIMESTAMP "
            "WHERE id = $2"
        )
        async with self.acquire() as conn:
            result = await conn.execute(
                query, json_module.dumps(container_updates), uuid_val
            )

        return result == "UPDATE 1"

    async def merge_thread_vm_context(
        self, thread_id: str, vm_updates: Dict[str, Any]
    ) -> bool:
        """Atomically merge updates into threads.metadata.vm.

        Same pattern as merge_vm_context() but targets the threads table
        metadata JSONB instead of jobs.context.

        Args:
            thread_id: Thread UUID as string
            vm_updates: Dictionary to merge into metadata.vm

        Returns:
            True if updated, False if not found
        """
        import json as json_module

        try:
            uuid_val = UUID(thread_id)
        except ValueError:
            return False

        query = (
            "UPDATE threads "
            "SET metadata = jsonb_set("
            "    COALESCE(metadata, '{}'::jsonb), "
            "    '{vm}', "
            "    COALESCE(metadata->'vm', '{}'::jsonb) || $1::jsonb"
            "), "
            "    last_activity = CURRENT_TIMESTAMP "
            "WHERE id = $2"
        )
        async with self.acquire() as conn:
            result = await conn.execute(query, json_module.dumps(vm_updates), uuid_val)

        return result == "UPDATE 1"

    async def merge_thread_snapshot_context(
        self, thread_id: str, snapshot_updates: Dict[str, Any]
    ) -> bool:
        """Atomically merge updates into threads.metadata.snapshot.

        Same pattern as merge_thread_workspace_context() but targets the
        snapshot key instead of workspace_container.

        Args:
            thread_id: Thread UUID as string
            snapshot_updates: Dictionary to merge into metadata.snapshot

        Returns:
            True if updated, False if not found
        """
        import json as json_module

        try:
            uuid_val = UUID(thread_id)
        except ValueError:
            return False

        query = (
            "UPDATE threads "
            "SET metadata = jsonb_set("
            "    COALESCE(metadata, '{}'::jsonb), "
            "    '{snapshot}', "
            "    COALESCE(metadata->'snapshot', '{}'::jsonb) || $1::jsonb"
            "), "
            "    last_activity = CURRENT_TIMESTAMP "
            "WHERE id = $2"
        )
        async with self.acquire() as conn:
            result = await conn.execute(
                query, json_module.dumps(snapshot_updates), uuid_val
            )

        return result == "UPDATE 1"

    async def get_job_progress(self, job_id: str) -> Dict[str, Any] | None:
        """Get detailed progress information for a job including ETA.

        Args:
            job_id: Job UUID as string

        Returns:
            Dict with progress details and ETA, or None if job not found
        """
        try:
            uuid_val = UUID(job_id)
        except ValueError:
            return None

        async with self.acquire() as conn:
            job = await conn.fetchrow(
                """
                SELECT id, description, status,
                       config_name, assigned_agent_id, created_at, updated_at, completed_at
                FROM jobs WHERE id = $1
                """,
                uuid_val,
            )
            if not job:
                return None

        # Calculate elapsed time
        created_at = job["created_at"]
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        elapsed = now - created_at
        elapsed_seconds = elapsed.total_seconds()

        return {
            "job_id": str(job["id"]),
            "status": job["status"],
            "progress_percent": 0,
            "elapsed_seconds": elapsed_seconds,
            "eta_seconds": None,
            "created_at": job["created_at"].isoformat() if job["created_at"] else None,
            "updated_at": job["updated_at"].isoformat() if job["updated_at"] else None,
            "completed_at": job["completed_at"].isoformat()
            if job["completed_at"]
            else None,
        }

    async def get_daily_statistics(self, days: int = 7) -> List[Dict[str, Any]]:
        """Get daily job statistics for the past N days.

        Args:
            days: Number of days to include

        Returns:
            List of daily statistics dictionaries
        """
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    DATE(created_at) as date,
                    COUNT(*) as jobs_created,
                    COUNT(*) FILTER (WHERE status = 'completed') as jobs_completed,
                    COUNT(*) FILTER (WHERE status = 'failed') as jobs_failed,
                    COUNT(*) FILTER (WHERE status = 'cancelled') as jobs_cancelled
                FROM jobs
                WHERE created_at > CURRENT_TIMESTAMP - INTERVAL '1 day' * $1
                GROUP BY DATE(created_at)
                ORDER BY date DESC
                """,
                days,
            )

        return [dict(row) for row in rows]

    async def get_job_statistics(self) -> Dict[str, int]:
        """Get overall job statistics.

        Returns:
            Dict with job counts by status
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) as total_jobs,
                    COUNT(*) FILTER (WHERE status = 'created') as created,
                    COUNT(*) FILTER (WHERE status = 'processing') as processing,
                    COUNT(*) FILTER (WHERE status = 'completed') as completed,
                    COUNT(*) FILTER (WHERE status = 'failed') as failed,
                    COUNT(*) FILTER (WHERE status = 'cancelled') as cancelled
                FROM jobs
                """
            )

        return dict(row) if row else {}

    async def detect_stuck_jobs(
        self, threshold_minutes: int = 60
    ) -> List[Dict[str, Any]]:
        """Detect jobs that appear to be stuck.

        A job is considered stuck if it's in 'processing' status but hasn't
        been updated within the threshold period.

        Args:
            threshold_minutes: Minutes without activity to consider stuck

        Returns:
            List of stuck job dictionaries with stuck reason
        """
        threshold = datetime.now(timezone.utc) - timedelta(minutes=threshold_minutes)

        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT j.id, j.description, j.status,
                       j.config_name, j.assigned_agent_id, j.created_at, j.updated_at
                FROM jobs j
                WHERE j.status = 'processing'
                AND j.updated_at < $1
                ORDER BY j.updated_at ASC
                """,
                threshold,
            )

        stuck_jobs = []
        for row in rows:
            job = dict(row)
            job["stuck_reason"] = "No recent activity"
            job["stuck_component"] = "unknown"
            stuck_jobs.append(job)

        return stuck_jobs

    # =========================================================================
    # AGENT OPERATIONS
    # =========================================================================

    async def register_agent(
        self,
        config_name: str,
        pod_ip: str,
        hostname: str | None = None,
        pod_port: int = 8001,
        pid: int | None = None,
        agent_mode: str = "worker",
        thread_id: str | None = None,
    ) -> Dict[str, Any]:
        """Register a new agent or update existing one.

        If an agent with the same hostname exists, update its pod_ip instead
        of creating a duplicate. This handles agent restarts with new IPs.

        Args:
            config_name: Agent configuration name
            pod_ip: Agent's IP address for receiving commands
            hostname: Optional hostname/pod name
            pod_port: Agent API port (default 8001)
            pid: Optional process ID

        Returns:
            Dict with agent_id and heartbeat_interval_seconds
        """
        async with self.acquire() as conn:
            # Check for existing agent with same hostname
            if hostname:
                existing = await conn.fetchrow(
                    "SELECT id FROM agents WHERE hostname = $1",
                    hostname,
                )
                if existing:
                    agent_id = existing["id"]

                    # Pause any processing jobs still assigned to this agent.
                    # The new instance won't know about them, so they'd be
                    # stuck in 'processing' forever without this.
                    await conn.execute(
                        """
                        UPDATE jobs
                        SET status = 'paused',
                            assigned_agent_id = NULL,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE assigned_agent_id = $1
                          AND status = 'processing'
                        """,
                        agent_id,
                    )

                    # Update existing agent's IP and reset status
                    await conn.execute(
                        """
                        UPDATE agents
                        SET pod_ip = $1,
                            pod_port = $2,
                            pid = $3,
                            config_name = $4,
                            status = 'booting',
                            current_job_id = NULL,
                            last_heartbeat = CURRENT_TIMESTAMP,
                            registered_at = CURRENT_TIMESTAMP,
                            agent_mode    = $6,
                            thread_id     = $7
                        WHERE id = $5
                        """,
                        pod_ip,
                        pod_port,
                        pid,
                        config_name,
                        agent_id,
                        agent_mode,
                        thread_id,
                    )
                    return {
                        "agent_id": str(agent_id),
                        "heartbeat_interval_seconds": 60,
                    }

            # Create new agent
            row = await conn.fetchrow(
                """
                INSERT INTO agents (config_name, hostname, pod_ip, pod_port, pid, agent_mode, thread_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id
                """,
                config_name,
                hostname,
                pod_ip,
                pod_port,
                pid,
                agent_mode,
                thread_id,
            )

            return {
                "agent_id": str(row["id"]),
                "heartbeat_interval_seconds": 60,
            }

    async def heartbeat(
        self,
        agent_id: str,
        status: str,
        current_job_id: str | None = None,
        metrics: Dict[str, Any] | None = None,
    ) -> Dict[str, Any] | None:
        """Update agent heartbeat and status.

        Detects working → ready transitions and sets last_completed_at for
        dispatch cooldown (but not after a pause — paused jobs clear the agent
        before the heartbeat fires).

        Args:
            agent_id: Agent UUID
            status: Agent status (booting, ready, working, completed, failed)
            current_job_id: Optional current job UUID
            metrics: Optional metrics dict to merge into metadata

        Returns:
            Dict with previous status (for transition detection) or None if not found
        """
        try:
            uuid_val = UUID(agent_id)
        except ValueError:
            return None

        job_uuid = UUID(current_job_id) if current_job_id else None

        async with self.acquire() as conn:
            # Fetch previous status for transition detection
            prev = await conn.fetchrow(
                "SELECT status FROM agents WHERE id = $1",
                uuid_val,
            )
            if not prev:
                return None

            prev_status = prev["status"]

            # Set last_completed_at when transitioning from working → ready/completed
            # This enables the dispatch cooldown (30s before next job assignment)
            set_completed = prev_status == "working" and status in (
                "ready",
                "completed",
            )

            if metrics:
                result = await conn.execute(
                    f"""
                    UPDATE agents
                    SET status = $1,
                        current_job_id = $2,
                        last_heartbeat = CURRENT_TIMESTAMP,
                        metadata = metadata || $3::jsonb
                        {"  , last_completed_at = CURRENT_TIMESTAMP" if set_completed else ""}
                    WHERE id = $4
                    """,
                    status,
                    job_uuid,
                    json.dumps(metrics),
                    uuid_val,
                )
            else:
                result = await conn.execute(
                    f"""
                    UPDATE agents
                    SET status = $1,
                        current_job_id = $2,
                        last_heartbeat = CURRENT_TIMESTAMP
                        {"  , last_completed_at = CURRENT_TIMESTAMP" if set_completed else ""}
                    WHERE id = $3
                    """,
                    status,
                    job_uuid,
                    uuid_val,
                )

            if result != "UPDATE 1":
                return None

            return {"previous_status": prev_status}

    async def list_agents(
        self,
        status: str | None = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """List agents with optional status filter.

        Args:
            status: Optional status filter
            limit: Maximum agents to return

        Returns:
            List of agent dicts
        """
        async with self.acquire() as conn:
            if status:
                rows = await conn.fetch(
                    """
                    SELECT id, config_name, hostname, pod_ip, pod_port, pid,
                           status, current_job_id, registered_at, last_heartbeat,
                           last_completed_at, metadata
                    FROM agents
                    WHERE status = $1
                    ORDER BY last_heartbeat DESC
                    LIMIT $2
                    """,
                    status,
                    limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, config_name, hostname, pod_ip, pod_port, pid,
                           status, current_job_id, registered_at, last_heartbeat,
                           last_completed_at, metadata
                    FROM agents
                    ORDER BY last_heartbeat DESC
                    LIMIT $1
                    """,
                    limit,
                )

        return [dict(row) for row in rows]

    async def get_agent(self, agent_id: str) -> Dict[str, Any] | None:
        """Get agent by ID.

        Args:
            agent_id: Agent UUID as string

        Returns:
            Agent dict or None if not found
        """
        try:
            uuid_val = UUID(agent_id)
        except ValueError:
            return None

        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, config_name, hostname, pod_ip, pod_port, pid,
                       status, current_job_id, registered_at, last_heartbeat, metadata
                FROM agents
                WHERE id = $1
                """,
                uuid_val,
            )

        return dict(row) if row else None

    async def delete_agent(self, agent_id: str) -> bool:
        """Delete (deregister) an agent.

        Args:
            agent_id: Agent UUID as string

        Returns:
            True if deleted, False if not found
        """
        try:
            uuid_val = UUID(agent_id)
        except ValueError:
            return False

        async with self.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM agents WHERE id = $1",
                uuid_val,
            )

        return result == "DELETE 1"

    async def mark_stale_agents_offline(self, timeout_minutes: int = 3) -> int:
        """Mark agents as offline if no heartbeat for timeout period.

        Args:
            timeout_minutes: Minutes without heartbeat before marking offline

        Returns:
            Number of agents marked offline
        """
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)

        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE agents
                SET status = 'offline'
                WHERE last_heartbeat < $1
                  AND status NOT IN ('offline', 'failed')
                """,
                cutoff,
            )

        # Parse result like "UPDATE 3" to get count
        if result.startswith("UPDATE "):
            return int(result.split()[1])
        return 0

    async def recover_orphaned_jobs(self) -> int:
        """Pause jobs still assigned to offline, deleted, or non-working agents.

        Finds jobs in 'processing' status that are orphaned because:
        - assigned_agent_id is NULL (agent row deleted via ON DELETE SET NULL)
        - assigned agent is offline (stale heartbeat)
        - assigned agent is online but NOT working on this job (e.g. the agent
          restarted and re-registered with the same hostname, or deregistered
          and a new agent took the same id)

        Sets them to 'paused' with cleared assigned_agent_id so the
        dispatcher can reassign them.

        Returns:
            Number of jobs recovered
        """
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE jobs
                SET status = 'paused',
                    assigned_agent_id = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE status = 'processing'
                  AND (
                      assigned_agent_id IS NULL
                      OR assigned_agent_id IN (
                          SELECT id FROM agents WHERE status = 'offline'
                      )
                      OR assigned_agent_id IN (
                          SELECT id FROM agents
                          WHERE status IN ('ready', 'booting')
                      )
                  )
                """
            )

            # Also clear stale agent assignments on waiting jobs.
            # Don't change status — the job must stay in 'waiting' until its
            # children complete and the unblock handler fires.
            result2 = await conn.execute(
                """
                UPDATE jobs
                SET assigned_agent_id = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE status = 'waiting'
                  AND assigned_agent_id IS NOT NULL
                  AND assigned_agent_id IN (
                      SELECT id FROM agents WHERE status = 'offline'
                  )
                """
            )

        count = 0
        if result.startswith("UPDATE "):
            count += int(result.split()[1])
        if result2.startswith("UPDATE "):
            count += int(result2.split()[1])
        return count

    async def get_ready_agents(self) -> List[Dict[str, Any]]:
        """Get all agents with 'ready' status.

        Returns:
            List of ready agent dicts
        """
        return await self.list_agents(status="ready")

    # =========================================================================
    # DISPATCHER QUERIES (Auto-Assignment)
    # =========================================================================

    async def get_dispatchable_jobs(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get jobs waiting for assignment, ordered by priority then creation time.

        Returns jobs in 'created' (new) or 'paused' (preempted) status
        that have no assigned agent.

        Args:
            limit: Maximum jobs to return

        Returns:
            List of job dicts ordered by priority DESC, created_at ASC
        """
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, description, status, config_name, config_override,
                       assigned_agent_id, user_id, project_id, parent_job_id,
                       priority, branch_name, context, created_at
                FROM jobs
                WHERE status IN ('created', 'paused')
                  AND assigned_agent_id IS NULL
                ORDER BY priority DESC, created_at ASC
                LIMIT $1
                """,
                limit,
            )
        return [dict(row) for row in rows]

    async def get_available_agents(
        self,
        limit: int = 20,
        cooldown_seconds: int = 30,
    ) -> List[Dict[str, Any]]:
        """Get agents available for job assignment.

        Returns agents with status='ready' that have passed the cooldown period
        since their last job completion.

        Args:
            limit: Maximum agents to return
            cooldown_seconds: Seconds to wait after last job completion

        Returns:
            List of agent dicts
        """
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, config_name, hostname, pod_ip, pod_port, pid,
                       status, current_job_id, registered_at, last_heartbeat,
                       last_completed_at, metadata
                FROM agents
                WHERE status = 'ready'
                  AND COALESCE(agent_mode, 'worker') = 'worker'
                  AND (last_completed_at IS NULL
                       OR NOW() - last_completed_at >= make_interval(secs => $1))
                ORDER BY last_heartbeat DESC
                LIMIT $2
                """,
                float(cooldown_seconds),
                limit,
            )
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Thread CRUD (persistent agent sessions)
    # ------------------------------------------------------------------

    async def create_thread(
        self,
        user_id: str | None = None,
        project_id: str | None = None,
        config_name: str = "defaults",
        permission_mode: str = "supervised",
        title: str = "Untitled Session",
    ) -> str:
        """Create a new persistent thread."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO threads (user_id, project_id, config_name, permission_mode, title)
                VALUES ($1, $2, $3, $4, $5) RETURNING id
                """,
                user_id,
                project_id,
                config_name,
                permission_mode,
                title,
            )
        return str(row["id"])

    async def get_thread(self, thread_id: str) -> Dict[str, Any] | None:
        """Get thread by ID."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM threads WHERE id = $1",
                thread_id,
            )
        return dict(row) if row else None

    async def list_threads(
        self,
        user_id: str | None = None,
        project_id: str | None = None,
        status: str | None = None,
    ) -> List[Dict[str, Any]]:
        """List threads with optional filters."""
        conditions = []
        params = []
        idx = 1

        if user_id:
            conditions.append(f"(user_id = ${idx} OR user_id IS NULL)")
            params.append(user_id)
            idx += 1
        if project_id:
            conditions.append(f"project_id = ${idx}")
            params.append(project_id)
            idx += 1
        if status:
            conditions.append(f"status = ${idx}")
            params.append(status)
            idx += 1

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        async with self.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM threads {where} ORDER BY created_at DESC LIMIT 50",
                *params,
            )
        return [dict(row) for row in rows]

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

    async def resume_thread(self, thread_id: str) -> None:
        """Resume an ended/idle thread — reset to 'created', clear stale agent."""
        async with self.acquire() as conn:
            await conn.execute(
                """
                UPDATE threads
                SET status        = 'created',
                    agent_id      = NULL,
                    ended_at      = NULL,
                    last_activity = CURRENT_TIMESTAMP
                WHERE id = $1
                """,
                thread_id,
            )

    async def delete_thread(self, thread_id: str) -> None:
        """Permanently delete a thread and its messages."""
        async with self.acquire() as conn:
            await conn.execute(
                "DELETE FROM thread_messages WHERE thread_id = $1",
                thread_id,
            )
            await conn.execute(
                "DELETE FROM threads WHERE id = $1",
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

    async def mark_orphaned_threads_idle(self) -> int:
        """Mark threads as idle if their bound agent is offline or deleted.

        Threads in 'created' or 'active' status whose agent_id is NULL
        (deleted) or bound to an offline agent are set to 'idle'.

        Returns:
            Number of threads marked idle.
        """
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE threads
                SET status        = 'idle',
                    last_activity = CURRENT_TIMESTAMP
                WHERE status IN ('created', 'active')
                  AND (
                    agent_id IS NULL
                        OR agent_id IN (SELECT id
                                        FROM agents
                                        WHERE status = 'offline')
                    )
                """
            )
        if result.startswith("UPDATE "):
            return int(result.split()[1])
        return 0

    async def update_thread_agent(self, thread_id: str, agent_id: str) -> None:
        """Bind an agent to a thread."""
        async with self.acquire() as conn:
            await conn.execute(
                "UPDATE threads SET agent_id = $2 WHERE id = $1",
                thread_id,
                agent_id,
            )

    # --- Thread message persistence ---

    async def save_thread_message(
        self,
        thread_id: str,
        role: str,
        content: Optional[str],
        tool_calls: Optional[Any] = None,
        turn_number: Optional[int] = None,
        metrics: Optional[dict] = None,
    ) -> str:
        """Save a message to thread_messages. Fire-and-forget safe."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO thread_messages (thread_id, role, content, tool_calls, turn_number, metrics)
                VALUES ($1, $2, $3, $4, $5, $6) RETURNING id
                """,
                thread_id,
                role,
                content,
                json.dumps(tool_calls) if tool_calls else None,
                turn_number,
                json.dumps(metrics) if metrics else None,
            )
            # Update thread activity + turn count
            await conn.execute(
                """
                UPDATE threads
                SET last_activity = CURRENT_TIMESTAMP,
                    total_turns   = GREATEST(total_turns, COALESCE($2, 0))
                WHERE id = $1
                """,
                thread_id,
                turn_number,
            )
        return str(row["id"])

    async def get_thread_messages_history(
        self,
        thread_id: str,
        limit: int = 200,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Load thread message history for session resume. Ordered by created_at ASC."""
        async with self.acquire() as conn:
            rows = await conn.fetch(
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

    async def get_thread_message_count(self, thread_id: str) -> int:
        """Get total message count for a thread."""
        async with self.acquire() as conn:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM thread_messages WHERE thread_id = $1",
                thread_id,
            )

    async def update_thread_tokens(self, thread_id: str, tokens: int) -> None:
        """Increment total token usage for a thread."""
        async with self.acquire() as conn:
            await conn.execute(
                """
                UPDATE threads
                SET total_tokens = total_tokens + $2
                WHERE id = $1
                """,
                thread_id,
                tokens,
            )

    async def get_preemption_candidates(self) -> List[Dict[str, Any]]:
        """Get running jobs ordered by priority ASC (lowest priority first).

        These are candidates for preemption when a higher-priority job needs
        an agent. Only returns jobs with an assigned agent.

        Returns:
            List of job dicts with their assigned agent info, lowest priority first
        """
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT j.id, j.description, j.status, j.config_name,
                       j.priority, j.assigned_agent_id, j.created_at,
                       a.pod_ip, a.pod_port, a.hostname AS agent_hostname
                FROM jobs j
                JOIN agents a ON j.assigned_agent_id = a.id
                WHERE j.status = 'processing'
                  AND j.assigned_agent_id IS NOT NULL
                ORDER BY j.priority ASC, j.created_at DESC
                """,
            )
        return [dict(row) for row in rows]

    # =========================================================================
    # DATASOURCE OPERATIONS
    # =========================================================================

    async def list_datasources(
        self,
        job_id: str | None = None,
        ds_type: str | None = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """List datasources with optional filters.

        Args:
            job_id: Filter by job ID. Use "global" for global-only datasources.
            ds_type: Filter by datasource type (e.g. 'neo4j', 'postgresql')
            limit: Maximum datasources to return

        Returns:
            List of datasource dicts
        """
        conditions = []
        values = []
        param_count = 0

        if job_id == "global":
            conditions.append("job_id IS NULL")
        elif job_id is not None:
            try:
                param_count += 1
                conditions.append(f"job_id = ${param_count}")
                values.append(UUID(job_id))
            except ValueError:
                return []

        if ds_type:
            param_count += 1
            conditions.append(f"type = ${param_count}")
            values.append(ds_type)

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        param_count += 1
        values.append(limit)

        async with self.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, name, description, type, connection_url, credentials,
                       read_only, job_id, created_at, updated_at
                FROM datasources
                {where_clause}
                ORDER BY created_at DESC
                LIMIT ${param_count}
                """,
                *values,
            )

        return [dict(row) for row in rows]

    async def get_datasource(self, datasource_id: str) -> Dict[str, Any] | None:
        """Get a single datasource by ID.

        Args:
            datasource_id: Datasource UUID as string

        Returns:
            Datasource dict or None if not found
        """
        try:
            uuid_val = UUID(datasource_id)
        except ValueError:
            return None

        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, name, description, type, connection_url, credentials,
                       read_only, job_id, created_at, updated_at
                FROM datasources
                WHERE id = $1
                """,
                uuid_val,
            )

        return dict(row) if row else None

    async def create_datasource(
        self,
        name: str,
        ds_type: str,
        connection_url: str,
        description: str | None = None,
        credentials: Dict[str, Any] | None = None,
        read_only: bool = True,
        job_id: str | None = None,
    ) -> Dict[str, Any]:
        """Create a new datasource.

        Args:
            name: User-provided label
            ds_type: Datasource type ('postgresql', 'neo4j', 'mongodb')
            connection_url: Full connection string
            description: What this datasource contains
            credentials: Additional auth details (JSONB)
            read_only: Whether the agent is allowed to write
            job_id: Job UUID (None for global)

        Returns:
            Created datasource dict

        Raises:
            asyncpg.UniqueViolationError: If a datasource of this type
                already exists for the given scope
        """
        job_uuid = UUID(job_id) if job_id else None

        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO datasources (name, description, type, connection_url,
                                         credentials, read_only, job_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id, name, description, type, connection_url, credentials,
                          read_only, job_id, created_at, updated_at
                """,
                name,
                description,
                ds_type,
                connection_url,
                json.dumps(credentials) if credentials else "{}",
                read_only,
                job_uuid,
            )

        return dict(row)

    async def update_datasource(
        self,
        datasource_id: str,
        name: str | None = None,
        description: str | None = None,
        connection_url: str | None = None,
        credentials: Dict[str, Any] | None = None,
        read_only: bool | None = None,
    ) -> bool:
        """Update a datasource.

        Args:
            datasource_id: Datasource UUID
            name: New name
            description: New description
            connection_url: New connection URL
            credentials: New credentials
            read_only: New read_only flag

        Returns:
            True if updated, False if not found
        """
        try:
            uuid_val = UUID(datasource_id)
        except ValueError:
            return False

        updates = []
        values = []
        param_count = 0

        if name is not None:
            param_count += 1
            updates.append(f"name = ${param_count}")
            values.append(name)

        if description is not None:
            param_count += 1
            updates.append(f"description = ${param_count}")
            values.append(description)

        if connection_url is not None:
            param_count += 1
            updates.append(f"connection_url = ${param_count}")
            values.append(connection_url)

        if credentials is not None:
            param_count += 1
            updates.append(f"credentials = ${param_count}")
            values.append(json.dumps(credentials))

        if read_only is not None:
            param_count += 1
            updates.append(f"read_only = ${param_count}")
            values.append(read_only)

        if not updates:
            return False

        param_count += 1
        values.append(uuid_val)

        query = f"UPDATE datasources SET {', '.join(updates)} WHERE id = ${param_count}"

        async with self.acquire() as conn:
            result = await conn.execute(query, *values)

        return result == "UPDATE 1"

    async def delete_datasource(self, datasource_id: str) -> bool:
        """Delete a datasource.

        Args:
            datasource_id: Datasource UUID

        Returns:
            True if deleted, False if not found
        """
        try:
            uuid_val = UUID(datasource_id)
        except ValueError:
            return False

        async with self.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM datasources WHERE id = $1",
                uuid_val,
            )

        return result == "DELETE 1"

    async def resolve_datasources_for_job(
        self, job_id: str, project_id: str | None = None
    ) -> List[Dict[str, Any]]:
        """Resolve datasources for a job (job > project > global).

        For each datasource type, returns the most specific one:
        job-specific first, then project-level, then global.

        Args:
            job_id: Job UUID
            project_id: Optional project UUID for project-level datasources

        Returns:
            List of resolved datasource dicts (one per type)
        """
        try:
            uuid_val = UUID(job_id)
        except ValueError:
            return []

        project_uuid = UUID(project_id) if project_id else None

        async with self.acquire() as conn:
            if project_uuid:
                rows = await conn.fetch(
                    """
                    SELECT DISTINCT ON (type)
                        id, name, description, type, connection_url, credentials,
                        read_only, job_id, project_id, created_at, updated_at
                    FROM datasources
                    WHERE job_id = $1 OR project_id = $2 OR (job_id IS NULL AND project_id IS NULL)
                    ORDER BY type,
                             CASE WHEN job_id IS NOT NULL THEN 0
                                  WHEN project_id IS NOT NULL THEN 1
                                  ELSE 2
                             END
                    """,
                    uuid_val,
                    project_uuid,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT DISTINCT ON (type)
                        id, name, description, type, connection_url, credentials,
                        read_only, job_id, project_id, created_at, updated_at
                    FROM datasources
                    WHERE job_id = $1 OR job_id IS NULL
                    ORDER BY type, job_id NULLS LAST
                    """,
                    uuid_val,
                )

        return [dict(row) for row in rows]

    async def upsert_default_datasource(
        self,
        name: str,
        ds_type: str,
        connection_url: str,
        credentials: Dict[str, Any] | None = None,
        read_only: bool = True,
    ) -> Dict[str, Any]:
        """Create or update a global (job_id=NULL) datasource.

        Used during init to seed default datasources from env vars.

        Args:
            name: Datasource label
            ds_type: Datasource type
            connection_url: Connection URL
            credentials: Additional auth details
            read_only: Read-only flag

        Returns:
            Created or updated datasource dict
        """
        creds_json = json.dumps(credentials) if credentials else "{}"

        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO datasources (name, type, connection_url, credentials, read_only, job_id, project_id)
                VALUES ($1, $2, $3, $4, $5, NULL, NULL)
                ON CONFLICT (type, COALESCE(job_id, '00000000-0000-0000-0000-000000000000'), COALESCE(project_id, '00000000-0000-0000-0000-000000000000'))
                DO UPDATE SET
                    name = EXCLUDED.name,
                    connection_url = EXCLUDED.connection_url,
                    credentials = EXCLUDED.credentials,
                    read_only = EXCLUDED.read_only
                RETURNING id, name, description, type, connection_url, credentials,
                          read_only, job_id, created_at, updated_at
                """,
                name,
                ds_type,
                connection_url,
                creds_json,
                read_only,
            )

        return dict(row)

    # =========================================================================
    # SESSION OPERATIONS
    # =========================================================================

    async def create_session(
        self,
        session_key: str,
        user_id: str,
        email: str,
        expires_at: datetime,
        csrf_token: str,
    ) -> None:
        """Create a new session.

        Args:
            session_key: Unique session key
            user_id: User UUID as string
            email: User's email
            expires_at: Session expiration timestamp
            csrf_token: CSRF token for this session
        """
        user_uuid = UUID(user_id) if isinstance(user_id, str) else user_id

        async with self.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO sessions (session_key, user_id, email, expires_at, csrf_token)
                VALUES ($1, $2, $3, $4, $5)
                """,
                session_key,
                user_uuid,
                email,
                expires_at,
                csrf_token,
            )

    async def get_session(self, session_key: str) -> Optional[Dict[str, Any]]:
        """Get a valid (non-expired) session by key.

        Args:
            session_key: Session key

        Returns:
            Session dict or None if not found/expired
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT session_key, user_id, email, created_at, expires_at,
                       last_activity, csrf_token
                FROM sessions
                WHERE session_key = $1 AND expires_at > NOW()
                """,
                session_key,
            )

        return dict(row) if row else None

    async def update_session_activity(self, session_key: str) -> None:
        """Update last_activity timestamp for a session."""
        async with self.acquire() as conn:
            await conn.execute(
                "UPDATE sessions SET last_activity = NOW() WHERE session_key = $1",
                session_key,
            )

    async def delete_session(self, session_key: str) -> None:
        """Delete a session by key."""
        async with self.acquire() as conn:
            await conn.execute(
                "DELETE FROM sessions WHERE session_key = $1",
                session_key,
            )

    async def delete_expired_sessions(self) -> None:
        """Delete all expired sessions."""
        async with self.acquire() as conn:
            result = await conn.execute("DELETE FROM sessions WHERE expires_at < NOW()")
            if result != "DELETE 0":
                logger.debug(f"Cleaned up expired sessions: {result}")

    async def delete_sessions_by_user(self, user_id: str) -> None:
        """Delete all sessions for a user."""
        user_uuid = UUID(user_id) if isinstance(user_id, str) else user_id

        async with self.acquire() as conn:
            await conn.execute(
                "DELETE FROM sessions WHERE user_id = $1",
                user_uuid,
            )

    # =========================================================================
    # Auth Tokens (verification codes, password reset tokens)
    # =========================================================================

    async def create_auth_token(
        self,
        email: str,
        token: str,
        token_type: str,
        user_id: str | None = None,
        expires_minutes: int = 30,
    ) -> None:
        """Create an auth token (verification or password reset)."""
        from datetime import datetime, timedelta, timezone

        user_uuid = UUID(user_id) if user_id else None
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=expires_minutes)

        async with self.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO auth_tokens (user_id, email, token, token_type, expires_at)
                VALUES ($1, $2, $3, $4, $5)
                """,
                user_uuid,
                email.lower(),
                token,
                token_type,
                expires_at,
            )

    async def get_auth_token(
        self, token: str, token_type: str
    ) -> Dict[str, Any] | None:
        """Get a valid (non-expired, unused) auth token."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, user_id, email, token, token_type, expires_at, used_at, created_at
                FROM auth_tokens
                WHERE token = $1 AND token_type = $2
                  AND expires_at > NOW() AND used_at IS NULL
                """,
                token,
                token_type,
            )
        return dict(row) if row else None

    async def mark_auth_token_used(self, token: str) -> None:
        """Mark an auth token as used."""
        async with self.acquire() as conn:
            await conn.execute(
                "UPDATE auth_tokens SET used_at = NOW() WHERE token = $1",
                token,
            )

    async def delete_auth_tokens_by_email(self, email: str, token_type: str) -> None:
        """Delete all tokens of a given type for an email (cleanup before issuing new one)."""
        async with self.acquire() as conn:
            await conn.execute(
                "DELETE FROM auth_tokens WHERE LOWER(email) = LOWER($1) AND token_type = $2",
                email,
                token_type,
            )

    async def delete_expired_auth_tokens(self) -> None:
        """Delete all expired auth tokens."""
        async with self.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM auth_tokens WHERE expires_at < NOW()"
            )
            if result != "DELETE 0":
                logger.debug(f"Cleaned up expired auth tokens: {result}")

    async def get_latest_auth_token_time(self, email: str, token_type: str):
        """Get the creation time of the most recent token for rate limiting."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT created_at FROM auth_tokens
                WHERE LOWER(email) = LOWER($1) AND token_type = $2
                ORDER BY created_at DESC LIMIT 1
                """,
                email,
                token_type,
            )
        return row["created_at"] if row else None

    # =========================================================================
    # User Auth Fields (password, email verification)
    # =========================================================================

    async def get_user_by_email_with_auth(self, email: str) -> Dict[str, Any] | None:
        """Get a user by email including auth fields (password_hash, email_verified)."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, display_name, avatar_color, email, default_project_id,
                       created_at, password_hash, email_verified, is_admin
                FROM users
                WHERE LOWER(email) = LOWER($1)
                """,
                email,
            )
        return dict(row) if row else None

    async def set_user_password(self, user_id: str, password_hash: str) -> bool:
        """Set or update a user's password hash."""
        user_uuid = UUID(user_id) if isinstance(user_id, str) else user_id
        async with self.acquire() as conn:
            result = await conn.execute(
                "UPDATE users SET password_hash = $1 WHERE id = $2",
                password_hash,
                user_uuid,
            )
        return result == "UPDATE 1"

    async def set_email_verified(self, user_id: str) -> bool:
        """Mark a user's email as verified."""
        user_uuid = UUID(user_id) if isinstance(user_id, str) else user_id
        async with self.acquire() as conn:
            result = await conn.execute(
                "UPDATE users SET email_verified = TRUE WHERE id = $1",
                user_uuid,
            )
        return result == "UPDATE 1"

    async def create_user_with_password(
        self,
        display_name: str,
        email: str,
        password_hash: str,
        avatar_color: str = "#89b4fa",
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        """Create a user with password (unverified) and their default project atomically."""
        async with self.acquire() as conn:
            async with conn.transaction():
                # Create project first
                project_row = await conn.fetchrow(
                    """
                    INSERT INTO projects (name, description, is_default)
                    VALUES ($1, $2, TRUE)
                    RETURNING id, name, description, goal, status, is_default,
                              default_config_name, default_config_override,
                              created_at, updated_at
                    """,
                    f"{display_name}'s Workspace",
                    f"Default workspace for {display_name}",
                )

                # Create user with password_hash and email_verified=FALSE
                user_row = await conn.fetchrow(
                    """
                    INSERT INTO users (display_name, avatar_color, email,
                                       default_project_id, password_hash, email_verified)
                    VALUES ($1, $2, $3, $4, $5, FALSE)
                    RETURNING id, display_name, avatar_color, email,
                              default_project_id, created_at
                    """,
                    display_name,
                    avatar_color,
                    email,
                    project_row["id"],
                    password_hash,
                )

                # Add user as project owner
                await conn.execute(
                    """
                    INSERT INTO project_members (project_id, user_id, role)
                    VALUES ($1, $2, 'owner')
                    """,
                    project_row["id"],
                    user_row["id"],
                )

        return dict(user_row), dict(project_row)

    async def migrate_existing_users_verified(self) -> None:
        """Mark existing users without a password as email_verified.

        Called during init to ensure existing users aren't locked out.
        """
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE users SET email_verified = TRUE
                WHERE email_verified = FALSE AND password_hash IS NULL
                """
            )
            if result != "UPDATE 0":
                logger.info(f"Migrated existing users to verified: {result}")

    # =========================================================================
    # MCP TOKEN OPERATIONS
    # =========================================================================

    async def create_mcp_token(
        self,
        user_id: str,
        name: str,
        token_hash: str,
        token_prefix: str,
        scope: str = "user",
        expires_at=None,
        origin: str | None = None,
    ) -> Dict[str, Any]:
        """Create a new MCP API token."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO mcp_tokens (user_id, name, token_hash, token_prefix, scope, expires_at, origin)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id, user_id, name, token_prefix, scope, origin,
                          expires_at, revoked_at, last_used_at, created_at
                """,
                user_id,
                name,
                token_hash,
                token_prefix,
                scope,
                expires_at,
                origin,
            )
            return dict(row)

    async def get_mcp_token_by_hash(self, token_hash: str) -> Dict[str, Any] | None:
        """Look up an active MCP token by its hash. Returns None if revoked/expired."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT t.id, t.user_id, t.name, t.token_prefix, t.scope,
                       t.expires_at, t.last_used_at, t.created_at,
                       u.display_name, u.email
                FROM mcp_tokens t
                JOIN users u ON u.id = t.user_id
                WHERE t.token_hash = $1
                  AND t.revoked_at IS NULL
                  AND (t.expires_at IS NULL OR t.expires_at > CURRENT_TIMESTAMP)
                """,
                token_hash,
            )
            return dict(row) if row else None

    async def list_mcp_tokens(self, user_id: str) -> List[Dict[str, Any]]:
        """List all MCP tokens for a user (excludes token_hash)."""
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, user_id, name, token_prefix, scope, origin,
                       expires_at, revoked_at, last_used_at, created_at
                FROM mcp_tokens
                WHERE user_id = $1
                ORDER BY created_at DESC
                """,
                user_id,
            )
            return [dict(r) for r in rows]

    async def revoke_mcp_token(self, token_id: str, user_id: str) -> bool:
        """Revoke an MCP token. Returns True if a token was revoked."""
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE mcp_tokens SET revoked_at = CURRENT_TIMESTAMP
                WHERE id = $1 AND user_id = $2 AND revoked_at IS NULL
                """,
                token_id,
                user_id,
            )
            return result == "UPDATE 1"

    async def update_mcp_token_last_used(self, token_hash: str) -> None:
        """Update the last_used_at timestamp for an MCP token."""
        async with self.acquire() as conn:
            await conn.execute(
                "UPDATE mcp_tokens SET last_used_at = CURRENT_TIMESTAMP WHERE token_hash = $1",
                token_hash,
            )

    async def cleanup_expired_mcp_tokens(self) -> None:
        """Delete expired and long-revoked MCP tokens."""
        async with self.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM mcp_tokens
                WHERE (expires_at IS NOT NULL AND expires_at < CURRENT_TIMESTAMP)
                   OR (revoked_at IS NOT NULL AND revoked_at < CURRENT_TIMESTAMP - INTERVAL '30 days')
                """
            )

    # =========================================================================
    # USER API KEY OPERATIONS
    # =========================================================================

    async def upsert_user_api_key(
        self,
        user_id: str,
        provider: str,
        api_key: str,
        key_prefix: str,
        label: str | None = None,
    ) -> Dict[str, Any]:
        """Create or replace a user's API key for a provider."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO user_api_keys (user_id, provider, api_key, key_prefix, label)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (user_id, provider) DO UPDATE
                SET api_key = EXCLUDED.api_key,
                    key_prefix = EXCLUDED.key_prefix,
                    label = EXCLUDED.label,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING id, user_id, provider, key_prefix, label, created_at, updated_at
                """,
                UUID(user_id),
                provider,
                api_key,
                key_prefix,
                label,
            )
            return dict(row)

    async def list_user_api_keys(self, user_id: str) -> List[Dict[str, Any]]:
        """List all API keys for a user (key_prefix only, no full key)."""
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, provider, key_prefix, label, created_at, updated_at
                FROM user_api_keys
                WHERE user_id = $1
                ORDER BY provider
                """,
                UUID(user_id),
            )
            return [dict(r) for r in rows]

    async def get_user_api_key(self, user_id: str, provider: str) -> str | None:
        """Get the full API key for a user+provider (for dispatch only)."""
        async with self.acquire() as conn:
            return await conn.fetchval(
                "SELECT api_key FROM user_api_keys WHERE user_id = $1 AND provider = $2",
                UUID(user_id),
                provider,
            )

    async def delete_user_api_key(self, user_id: str, provider: str) -> bool:
        """Delete a user's API key for a provider."""
        async with self.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM user_api_keys WHERE user_id = $1 AND provider = $2",
                UUID(user_id),
                provider,
            )
            return result == "DELETE 1"

    # =========================================================================
    # PROJECT API KEY OPERATIONS
    # =========================================================================

    async def upsert_project_api_key(
        self,
        project_id: str,
        provider: str,
        api_key: str,
        key_prefix: str,
        label: str | None = None,
    ) -> Dict[str, Any]:
        """Create or replace a project's API key for a provider."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO project_api_keys (project_id, provider, api_key, key_prefix, label)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (project_id, provider) DO UPDATE
                SET api_key = EXCLUDED.api_key,
                    key_prefix = EXCLUDED.key_prefix,
                    label = EXCLUDED.label,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING id, project_id, provider, key_prefix, label, created_at, updated_at
                """,
                UUID(project_id),
                provider,
                api_key,
                key_prefix,
                label,
            )
            return dict(row)

    async def list_project_api_keys(self, project_id: str) -> List[Dict[str, Any]]:
        """List all API keys for a project (key_prefix only, no full key)."""
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, provider, key_prefix, label, created_at, updated_at
                FROM project_api_keys
                WHERE project_id = $1
                ORDER BY provider
                """,
                UUID(project_id),
            )
            return [dict(r) for r in rows]

    async def get_project_api_key(self, project_id: str, provider: str) -> str | None:
        """Get the full API key for a project+provider (for dispatch only)."""
        async with self.acquire() as conn:
            return await conn.fetchval(
                "SELECT api_key FROM project_api_keys WHERE project_id = $1 AND provider = $2",
                UUID(project_id),
                provider,
            )

    async def delete_project_api_key(self, project_id: str, provider: str) -> bool:
        """Delete a project's API key for a provider."""
        async with self.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM project_api_keys WHERE project_id = $1 AND provider = $2",
                UUID(project_id),
                provider,
            )
            return result == "DELETE 1"

    # =========================================================================
    # API KEY RESOLUTION (for job dispatch)
    # =========================================================================

    async def resolve_api_keys_for_job(
        self,
        user_id: str | None,
        project_id: str | None,
    ) -> Dict[str, str]:
        """Resolve all API keys for a job (user > project fallback).

        Returns dict mapping provider -> api_key for all providers
        where at least one key exists.
        """
        resolved: Dict[str, str] = {}

        # Project keys first (lower priority — user keys will override)
        if project_id:
            async with self.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT provider, api_key FROM project_api_keys WHERE project_id = $1",
                    UUID(project_id),
                )
                for row in rows:
                    resolved[row["provider"]] = row["api_key"]

        # User keys override project keys
        if user_id:
            async with self.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT provider, api_key FROM user_api_keys WHERE user_id = $1",
                    UUID(user_id),
                )
                for row in rows:
                    resolved[row["provider"]] = row["api_key"]

        return resolved

    # =========================================================================
    # USER SETTINGS OPERATIONS
    # =========================================================================

    async def get_user_settings(self, user_id: str) -> Dict[str, Any]:
        """Get user preferences/settings. Returns empty dict if unset."""
        async with self.acquire() as conn:
            val = await conn.fetchval(
                "SELECT COALESCE(settings, '{}') FROM users WHERE id = $1",
                UUID(user_id),
            )
            if val is None:
                return {}
            return json.loads(val) if isinstance(val, str) else val

    async def update_user_settings(
        self, user_id: str, settings: Dict[str, Any]
    ) -> bool:
        """Merge settings into user's settings JSONB (patch semantics).

        Keys set to null are removed from the settings object.
        """
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE users
                SET settings = jsonb_strip_nulls(COALESCE(settings, '{}'::jsonb) || $2::jsonb)
                WHERE id = $1
                """,
                UUID(user_id),
                json.dumps(settings),
            )
            return result == "UPDATE 1"

    # =========================================================================
    # USER OPERATIONS
    # =========================================================================

    async def list_users(self, limit: int = 100) -> List[Dict[str, Any]]:
        """List all users.

        Args:
            limit: Maximum users to return

        Returns:
            List of user dicts ordered by created_at ASC
        """
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, display_name, avatar_color, email, default_project_id,
                       is_admin, created_at
                FROM users
                ORDER BY created_at ASC
                LIMIT $1
                """,
                limit,
            )

        return [dict(row) for row in rows]

    async def get_user(self, user_id: str) -> Dict[str, Any] | None:
        """Get a single user by ID.

        Args:
            user_id: User UUID as string

        Returns:
            User dict or None if not found
        """
        try:
            uuid_val = UUID(user_id)
        except ValueError:
            return None

        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, display_name, avatar_color, email, default_project_id,
                       is_admin, keycloak_sub, created_at
                FROM users
                WHERE id = $1
                """,
                uuid_val,
            )

        return dict(row) if row else None

    async def get_user_by_keycloak_sub(self, sub: str) -> Dict[str, Any] | None:
        """Get a user by Keycloak subject ID.

        Args:
            sub: Keycloak user subject (UUID from the `sub` claim)

        Returns:
            User dict or None if not found
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, display_name, avatar_color, email, default_project_id,
                       is_admin, keycloak_sub, created_at
                FROM users
                WHERE keycloak_sub = $1
                """,
                sub,
            )

        return dict(row) if row else None

    async def upsert_user_from_oidc(
        self,
        sub: str,
        email: str,
        display_name: str,
        is_admin: bool = False,
    ) -> Dict[str, Any]:
        """Create or update a user from OIDC claims (JIT provisioning).

        On first OIDC login, tries to match an existing user by email and link
        the keycloak_sub. If no email match, creates a new user. On subsequent
        logins, updates display_name and is_admin from the token claims.

        Also creates a default project for newly provisioned users.

        Args:
            sub: Keycloak subject ID
            email: Email from OIDC claims
            display_name: Display name from OIDC claims
            is_admin: Whether the user has the admin realm role

        Returns:
            Full user dict
        """
        async with self.acquire() as conn:
            # Try to link to existing user by email (handles pre-seeded admin)
            if email:
                existing = await conn.fetchrow(
                    """
                    SELECT id, display_name, avatar_color, email, default_project_id,
                           is_admin, keycloak_sub, created_at
                    FROM users
                    WHERE LOWER(email) = LOWER($1) AND keycloak_sub IS NULL
                    """,
                    email,
                )
                if existing:
                    await conn.execute(
                        "UPDATE users SET keycloak_sub = $1, is_admin = $2 WHERE id = $3",
                        sub,
                        is_admin,
                        existing["id"],
                    )
                    result = dict(existing)
                    result["keycloak_sub"] = sub
                    result["is_admin"] = is_admin
                    return result

            # Check if keycloak_sub already linked (concurrent request)
            existing_sub = await conn.fetchrow(
                """
                SELECT id, display_name, avatar_color, email, default_project_id,
                       is_admin, keycloak_sub, created_at
                FROM users WHERE keycloak_sub = $1
                """,
                sub,
            )
            if existing_sub:
                return dict(existing_sub)

            # Create new user + project atomically (constraint requires default_project_id)
            try:
                row = await conn.fetchrow(
                    """
                    WITH new_project AS (
                        INSERT INTO projects (name, description, is_default)
                        VALUES ($1 || '''s Project', 'Default project', true)
                        RETURNING id
                    ),
                    new_user AS (
                        INSERT INTO users (display_name, avatar_color, email, is_admin,
                                          keycloak_sub, default_project_id)
                        VALUES ($1, '#89b4fa', $2, $3, $4, (SELECT id FROM new_project))
                        RETURNING id, display_name, avatar_color, email, default_project_id,
                                  is_admin, keycloak_sub, created_at
                    ),
                    membership AS (
                        INSERT INTO project_members (project_id, user_id, role)
                        SELECT (SELECT id FROM new_project), id, 'owner'
                        FROM new_user
                    )
                    SELECT * FROM new_user
                    """,
                    display_name,
                    email,
                    is_admin,
                    sub,
                )
                return dict(row)
            except Exception as e:
                if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                    # Race condition: concurrent request already created this user.
                    # Retry lookup by keycloak_sub or email.
                    retry = await conn.fetchrow(
                        """
                        SELECT id, display_name, avatar_color, email, default_project_id,
                               is_admin, keycloak_sub, created_at
                        FROM users WHERE keycloak_sub = $1 OR LOWER(email) = LOWER($2)
                        LIMIT 1
                        """,
                        sub,
                        email,
                    )
                    if retry:
                        return dict(retry)
                raise

    async def get_user_by_email(self, email: str) -> Dict[str, Any] | None:
        """Get a user by email (case-insensitive).

        Args:
            email: Email address to look up

        Returns:
            User dict or None if not found
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, display_name, avatar_color, email, default_project_id,
                       is_admin, keycloak_sub, created_at
                FROM users
                WHERE LOWER(email) = LOWER($1)
                """,
                email,
            )

        return dict(row) if row else None

    async def create_user(
        self,
        display_name: str,
        avatar_color: str = "#89b4fa",
        email: str | None = None,
    ) -> Dict[str, Any]:
        """Create a new user.

        Args:
            display_name: User's display name
            avatar_color: Hex color for avatar dot
            email: Optional email address

        Returns:
            Created user dict
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO users (display_name, avatar_color, email)
                VALUES ($1, $2, $3)
                RETURNING id, display_name, avatar_color, email, created_at
                """,
                display_name,
                avatar_color,
                email,
            )

        return dict(row)

    async def create_user_with_default_project(
        self,
        display_name: str,
        avatar_color: str = "#89b4fa",
        email: str | None = None,
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        """Create a user and their default project atomically.

        Inserts both user and project in a single transaction so the
        NOT NULL constraint on default_project_id is never violated.

        Args:
            display_name: User's display name
            avatar_color: Hex color for avatar dot
            email: Optional email address

        Returns:
            Tuple of (user dict, project dict)
        """
        async with self.acquire() as conn:
            async with conn.transaction():
                # Create project first
                project_row = await conn.fetchrow(
                    """
                    INSERT INTO projects (name, description, is_default)
                    VALUES ($1, $2, TRUE)
                    RETURNING id, name, description, goal, status, is_default,
                              default_config_name, default_config_override,
                              created_at, updated_at
                    """,
                    f"{display_name}'s Workspace",
                    f"Default workspace for {display_name}",
                )

                # Create user with default_project_id set
                user_row = await conn.fetchrow(
                    """
                    INSERT INTO users (display_name, avatar_color, email, default_project_id)
                    VALUES ($1, $2, $3, $4)
                    RETURNING id, display_name, avatar_color, email,
                              default_project_id, created_at
                    """,
                    display_name,
                    avatar_color,
                    email,
                    project_row["id"],
                )

                # Add user as project owner
                await conn.execute(
                    """
                    INSERT INTO project_members (project_id, user_id, role)
                    VALUES ($1, $2, 'owner')
                    """,
                    project_row["id"],
                    user_row["id"],
                )

        return dict(user_row), dict(project_row)

    async def update_user(
        self,
        user_id: str,
        display_name: str | None = None,
        avatar_color: str | None = None,
        email: str | None = None,
    ) -> bool:
        """Update a user.

        Args:
            user_id: User UUID
            display_name: New display name
            avatar_color: New avatar color
            email: New email address

        Returns:
            True if updated, False if not found
        """
        try:
            uuid_val = UUID(user_id)
        except ValueError:
            return False

        updates = []
        values = []
        param_count = 0

        if display_name is not None:
            param_count += 1
            updates.append(f"display_name = ${param_count}")
            values.append(display_name)

        if avatar_color is not None:
            param_count += 1
            updates.append(f"avatar_color = ${param_count}")
            values.append(avatar_color)

        if email is not None:
            param_count += 1
            updates.append(f"email = ${param_count}")
            values.append(email)

        if not updates:
            return False

        param_count += 1
        values.append(uuid_val)

        query = f"UPDATE users SET {', '.join(updates)} WHERE id = ${param_count}"

        async with self.acquire() as conn:
            result = await conn.execute(query, *values)

        return result == "UPDATE 1"

    async def delete_user(self, user_id: str) -> bool:
        """Delete a user.

        Args:
            user_id: User UUID

        Returns:
            True if deleted, False if not found
        """
        try:
            uuid_val = UUID(user_id)
        except ValueError:
            return False

        async with self.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM users WHERE id = $1",
                uuid_val,
            )

        return result == "DELETE 1"

    async def upsert_default_user(
        self,
        display_name: str,
        avatar_color: str = "#89b4fa",
        email: str | None = None,
        is_admin: bool = False,
        password_hash: str | None = None,
        email_verified: bool = False,
    ) -> Dict[str, Any]:
        """Create a default user if one with the same display_name doesn't exist.

        Used during init seeding. If the user exists, updates email (if NULL)
        and is_admin (if changed). Supports optional password and email_verified
        for production-mode admin seeding.

        Args:
            display_name: User's display name
            avatar_color: Hex color for avatar dot
            email: Optional email address
            is_admin: Whether this user is an admin
            password_hash: Pre-hashed password (for production mode admin)
            email_verified: Whether email is verified

        Returns:
            Existing or newly created user dict
        """
        async with self.acquire() as conn:
            # Check if user with this name already exists
            existing = await conn.fetchrow(
                "SELECT id, display_name, avatar_color, email, is_admin, created_at FROM users WHERE display_name = $1",
                display_name,
            )
            if existing:
                updates = []
                params = []
                idx = 1
                # Update email if provided and different
                if email and email != existing.get("email"):
                    updates.append(f"email = ${idx}")
                    params.append(email)
                    idx += 1
                # Update is_admin if changed
                if is_admin != existing["is_admin"]:
                    updates.append(f"is_admin = ${idx}")
                    params.append(is_admin)
                    idx += 1
                # Update password_hash if provided
                if password_hash:
                    updates.append(f"password_hash = ${idx}")
                    params.append(password_hash)
                    idx += 1
                    updates.append(f"email_verified = ${idx}")
                    params.append(email_verified)
                    idx += 1
                if updates:
                    params.append(existing["id"])
                    await conn.execute(
                        f"UPDATE users SET {', '.join(updates)} WHERE id = ${idx}",
                        *params,
                    )
                result = {**dict(existing), "is_admin": is_admin}
                if email and existing["email"] is None:
                    result["email"] = email
                return result

            # Create new user
            row = await conn.fetchrow(
                """
                INSERT INTO users (display_name, avatar_color, email, is_admin,
                                   password_hash, email_verified)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id, display_name, avatar_color, email, is_admin, created_at
                """,
                display_name,
                avatar_color,
                email,
                is_admin,
                password_hash,
                email_verified,
            )

        return dict(row)

    async def get_admin_user(self) -> Dict[str, Any] | None:
        """Get the first admin user."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE is_admin = TRUE LIMIT 1"
            )
            return dict(row) if row else None

    # =========================================================================
    # PROJECT OPERATIONS
    # =========================================================================

    async def create_project(
        self,
        name: str,
        description: str | None = None,
        goal: str | None = None,
        is_default: bool = False,
        default_config_name: str | None = None,
        default_config_override: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Create a new project.

        Args:
            name: Project name
            description: What this project is about
            goal: Success criteria
            is_default: Whether this is a user's default project
            default_config_name: Default agent config for new jobs
            default_config_override: Default config overrides for new jobs

        Returns:
            Created project dict
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO projects (name, description, goal, is_default,
                                      default_config_name, default_config_override)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id, name, description, goal, status, is_default,
                          default_config_name, default_config_override,
                          created_at, updated_at
                """,
                name,
                description,
                goal,
                is_default,
                default_config_name,
                json.dumps(default_config_override)
                if default_config_override
                else None,
            )

        return dict(row)

    async def get_project(self, project_id: str) -> Dict[str, Any] | None:
        """Get a single project by ID.

        Args:
            project_id: Project UUID as string

        Returns:
            Project dict or None if not found
        """
        try:
            uuid_val = UUID(project_id)
        except ValueError:
            return None

        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, name, description, goal, status, is_default,
                       default_config_name, default_config_override,
                       created_at, updated_at
                FROM projects
                WHERE id = $1
                """,
                uuid_val,
            )

        return dict(row) if row else None

    async def get_projects_for_user(
        self, user_id: str, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Get all projects a user is a member of.

        Args:
            user_id: User UUID as string
            limit: Maximum projects to return

        Returns:
            List of project dicts with aggregate counts, ordered by
            is_default DESC, updated_at DESC
        """
        try:
            uuid_val = UUID(user_id)
        except ValueError:
            return []

        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT p.id, p.name, p.description, p.goal, p.status,
                       p.is_default, p.default_config_name,
                       p.created_at, p.updated_at,
                       pm.role AS user_role,
                       (SELECT COUNT(*) FROM jobs j WHERE j.project_id = p.id) AS job_count,
                       (SELECT COUNT(*) FROM project_repositories pr WHERE pr.project_id = p.id) AS repo_count,
                       (SELECT COUNT(*) FROM project_members pm2 WHERE pm2.project_id = p.id) AS member_count
                FROM projects p
                JOIN project_members pm ON p.id = pm.project_id
                WHERE pm.user_id = $1
                ORDER BY p.is_default DESC, p.updated_at DESC
                LIMIT $2
                """,
                uuid_val,
                limit,
            )

        return [dict(row) for row in rows]

    async def update_project(self, project_id: str, **kwargs) -> bool:
        """Update a project.

        Args:
            project_id: Project UUID
            **kwargs: Fields to update (name, description, goal, status,
                      default_config_name, default_config_override)

        Returns:
            True if updated, False if not found
        """
        try:
            uuid_val = UUID(project_id)
        except ValueError:
            return False

        allowed_fields = {
            "name",
            "description",
            "goal",
            "status",
            "default_config_name",
            "default_config_override",
        }

        updates = []
        values = []
        param_count = 0

        for key, value in kwargs.items():
            if key not in allowed_fields or value is None:
                continue
            param_count += 1
            updates.append(f"{key} = ${param_count}")
            if key == "default_config_override":
                values.append(json.dumps(value) if isinstance(value, dict) else value)
            else:
                values.append(value)

        if not updates:
            return False

        param_count += 1
        values.append(uuid_val)

        query = f"UPDATE projects SET {', '.join(updates)} WHERE id = ${param_count}"

        async with self.acquire() as conn:
            result = await conn.execute(query, *values)

        return result == "UPDATE 1"

    async def delete_project(self, project_id: str) -> bool:
        """Delete a project (cascades to members, repos).

        Args:
            project_id: Project UUID

        Returns:
            True if deleted, False if not found
        """
        try:
            uuid_val = UUID(project_id)
        except ValueError:
            return False

        async with self.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM projects WHERE id = $1",
                uuid_val,
            )

        return result == "DELETE 1"

    # -- Project Members --

    async def add_project_member(
        self,
        project_id: str,
        user_id: str,
        role: str = "editor",
    ) -> Dict[str, Any]:
        """Add a member to a project.

        Args:
            project_id: Project UUID
            user_id: User UUID
            role: Member role (owner, editor, viewer)

        Returns:
            Member dict with user info
        """
        project_uuid = UUID(project_id)
        user_uuid = UUID(user_id)

        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO project_members (project_id, user_id, role)
                VALUES ($1, $2, $3)
                RETURNING project_id, user_id, role, added_at AS joined_at
                """,
                project_uuid,
                user_uuid,
                role,
            )

            # Fetch user info for display
            user_row = await conn.fetchrow(
                "SELECT display_name, avatar_color FROM users WHERE id = $1",
                user_uuid,
            )

        result = dict(row)
        if user_row:
            result["display_name"] = user_row["display_name"]
            result["avatar_color"] = user_row["avatar_color"]

        return result

    async def get_project_members(self, project_id: str) -> List[Dict[str, Any]]:
        """Get all members of a project with user info.

        Args:
            project_id: Project UUID

        Returns:
            List of member dicts with user display info
        """
        try:
            uuid_val = UUID(project_id)
        except ValueError:
            return []

        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT pm.project_id, pm.user_id, pm.role,
                       pm.added_at AS joined_at,
                       u.display_name, u.avatar_color, u.email
                FROM project_members pm
                JOIN users u ON pm.user_id = u.id
                WHERE pm.project_id = $1
                ORDER BY pm.added_at ASC
                """,
                uuid_val,
            )

        return [dict(row) for row in rows]

    async def get_user_role_in_project(
        self, project_id: str, user_id: str
    ) -> str | None:
        """Get a user's role in a project.

        Args:
            project_id: Project UUID
            user_id: User UUID

        Returns:
            Role string or None if not a member
        """
        try:
            project_uuid = UUID(project_id)
            user_uuid = UUID(user_id)
        except ValueError:
            return None

        async with self.acquire() as conn:
            return await conn.fetchval(
                "SELECT role FROM project_members WHERE project_id = $1 AND user_id = $2",
                project_uuid,
                user_uuid,
            )

    async def update_project_member_role(
        self, project_id: str, user_id: str, role: str
    ) -> bool:
        """Update a member's role in a project.

        Args:
            project_id: Project UUID
            user_id: User UUID
            role: New role (owner, editor, viewer)

        Returns:
            True if updated, False if not found
        """
        try:
            project_uuid = UUID(project_id)
            user_uuid = UUID(user_id)
        except ValueError:
            return False

        async with self.acquire() as conn:
            result = await conn.execute(
                "UPDATE project_members SET role = $1 WHERE project_id = $2 AND user_id = $3",
                role,
                project_uuid,
                user_uuid,
            )

        return result == "UPDATE 1"

    async def remove_project_member(self, project_id: str, user_id: str) -> bool:
        """Remove a member from a project.

        Args:
            project_id: Project UUID
            user_id: User UUID

        Returns:
            True if removed, False if not found
        """
        try:
            project_uuid = UUID(project_id)
            user_uuid = UUID(user_id)
        except ValueError:
            return False

        async with self.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM project_members WHERE project_id = $1 AND user_id = $2",
                project_uuid,
                user_uuid,
            )

        return result == "DELETE 1"

    # -- Project Repositories --

    async def add_project_repository(
        self,
        project_id: str,
        name: str,
        repo_url: str,
        role: str = "source",
        description: str | None = None,
        credentials: Dict[str, Any] | None = None,
        read_only: bool = False,
        is_managed: bool = False,
        branch: str = "main",
        clone_path: str | None = None,
    ) -> Dict[str, Any]:
        """Add a repository to a project.

        Args:
            project_id: Project UUID
            name: Human-readable label
            repo_url: Git clone URL
            role: Repository role (jobs, source, reference)
            description: What this repo contains
            credentials: Auth for external repos
            read_only: Whether agents can push
            is_managed: True if created by us (Gitea)
            branch: Default branch to clone from
            clone_path: Subdirectory name in workspace

        Returns:
            Created repository dict
        """
        project_uuid = UUID(project_id)

        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO project_repositories
                    (project_id, name, repo_url, role, description, credentials,
                     read_only, is_managed, branch, clone_path)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                RETURNING id, project_id, name, description, repo_url, credentials,
                          role, read_only, is_managed, branch, clone_path,
                          created_at, updated_at
                """,
                project_uuid,
                name,
                repo_url,
                role,
                description,
                json.dumps(credentials) if credentials else "{}",
                read_only,
                is_managed,
                branch,
                clone_path,
            )

        return dict(row)

    async def get_project_repositories(
        self, project_id: str, role: str | None = None
    ) -> List[Dict[str, Any]]:
        """Get repositories for a project.

        Args:
            project_id: Project UUID
            role: Optional role filter (jobs, source, reference)

        Returns:
            List of repository dicts
        """
        try:
            uuid_val = UUID(project_id)
        except ValueError:
            return []

        async with self.acquire() as conn:
            if role:
                rows = await conn.fetch(
                    """
                    SELECT id, project_id, name, description, repo_url, credentials,
                           role, read_only, is_managed, branch, clone_path,
                           created_at, updated_at
                    FROM project_repositories
                    WHERE project_id = $1 AND role = $2
                    ORDER BY created_at ASC
                    """,
                    uuid_val,
                    role,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, project_id, name, description, repo_url, credentials,
                           role, read_only, is_managed, branch, clone_path,
                           created_at, updated_at
                    FROM project_repositories
                    WHERE project_id = $1
                    ORDER BY created_at ASC
                    """,
                    uuid_val,
                )

        return [dict(row) for row in rows]

    async def get_project_repository(self, repo_id: str) -> Dict[str, Any] | None:
        """Get a single project repository by ID.

        Args:
            repo_id: Repository UUID

        Returns:
            Repository dict or None if not found
        """
        try:
            uuid_val = UUID(repo_id)
        except ValueError:
            return None

        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, project_id, name, description, repo_url, credentials,
                       role, read_only, is_managed, branch, clone_path,
                       created_at, updated_at
                FROM project_repositories
                WHERE id = $1
                """,
                uuid_val,
            )

        return dict(row) if row else None

    async def update_project_repository(self, repo_id: str, **kwargs) -> bool:
        """Update a project repository.

        Args:
            repo_id: Repository UUID
            **kwargs: Fields to update (name, description, read_only, branch, clone_path)

        Returns:
            True if updated, False if not found
        """
        try:
            uuid_val = UUID(repo_id)
        except ValueError:
            return False

        allowed_fields = {"name", "description", "read_only", "branch", "clone_path"}

        updates = []
        values = []
        param_count = 0

        for key, value in kwargs.items():
            if key not in allowed_fields or value is None:
                continue
            param_count += 1
            updates.append(f"{key} = ${param_count}")
            values.append(value)

        if not updates:
            return False

        param_count += 1
        values.append(uuid_val)

        query = f"UPDATE project_repositories SET {', '.join(updates)} WHERE id = ${param_count}"

        async with self.acquire() as conn:
            result = await conn.execute(query, *values)

        return result == "UPDATE 1"

    async def remove_project_repository(self, repo_id: str) -> Dict[str, Any] | None:
        """Remove a project repository. Returns the deleted row for cleanup.

        Args:
            repo_id: Repository UUID

        Returns:
            Deleted repository dict (caller needs is_managed for Gitea cleanup),
            or None if not found
        """
        try:
            uuid_val = UUID(repo_id)
        except ValueError:
            return None

        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                DELETE FROM project_repositories WHERE id = $1
                RETURNING id, project_id, name, repo_url, role, is_managed
                """,
                uuid_val,
            )

        return dict(row) if row else None

    # -- Default Project Lifecycle --

    async def create_default_project_for_user(
        self, user_id: str, display_name: str
    ) -> Dict[str, Any]:
        """Create a default project for a user.

        Creates the project, adds the user as owner, and updates
        users.default_project_id.

        Args:
            user_id: User UUID as string
            display_name: User's display name (for project naming)

        Returns:
            Created project dict
        """
        user_uuid = UUID(user_id)

        async with self.acquire() as conn:
            # Create the project
            project_row = await conn.fetchrow(
                """
                INSERT INTO projects (name, description, is_default)
                VALUES ($1, $2, TRUE)
                RETURNING id, name, description, goal, status, is_default,
                          default_config_name, default_config_override,
                          created_at, updated_at
                """,
                f"{display_name}'s Workspace",
                f"Default workspace for {display_name}",
            )

            project_id = project_row["id"]

            # Add user as owner
            await conn.execute(
                """
                INSERT INTO project_members (project_id, user_id, role)
                VALUES ($1, $2, 'owner')
                """,
                project_id,
                user_uuid,
            )

            # Update user's default_project_id
            await conn.execute(
                "UPDATE users SET default_project_id = $1 WHERE id = $2",
                project_id,
                user_uuid,
            )

        return dict(project_row)

    async def get_user_default_project(self, user_id: str) -> Dict[str, Any] | None:
        """Get a user's default project.

        Args:
            user_id: User UUID as string

        Returns:
            Project dict or None if no default project
        """
        try:
            uuid_val = UUID(user_id)
        except ValueError:
            return None

        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT p.id, p.name, p.description, p.goal, p.status,
                       p.is_default, p.default_config_name,
                       p.default_config_override, p.created_at, p.updated_at
                FROM projects p
                JOIN users u ON u.default_project_id = p.id
                WHERE u.id = $1
                """,
                uuid_val,
            )

        return dict(row) if row else None

    # =========================================================================
    # BUILDER SESSION OPERATIONS
    # =========================================================================

    async def create_builder_session(
        self,
        expert_id: str | None = None,
        user_id: str | None = None,
    ) -> Dict[str, Any]:
        """Create a new builder chat session.

        Args:
            expert_id: Optional expert ID used as starting point
            user_id: Optional user UUID who created this session

        Returns:
            Created session dict with id, expert_id, user_id, created_at, updated_at
        """
        user_uuid = UUID(user_id) if user_id else None

        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO builder_sessions (expert_id, user_id)
                VALUES ($1, $2)
                RETURNING id, job_id, expert_id, user_id, created_at, updated_at, summary
                """,
                expert_id,
                user_uuid,
            )

        return dict(row)

    async def get_builder_session(self, session_id: str) -> Dict[str, Any] | None:
        """Get a builder session by ID.

        Args:
            session_id: Session UUID as string

        Returns:
            Session dict or None if not found
        """
        try:
            uuid_val = UUID(session_id)
        except ValueError:
            return None

        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, job_id, expert_id, created_at, updated_at, summary
                FROM builder_sessions
                WHERE id = $1
                """,
                uuid_val,
            )

        return dict(row) if row else None

    async def update_builder_session_job(self, session_id: str, job_id: str) -> bool:
        """Link a builder session to a job after job creation.

        Args:
            session_id: Session UUID
            job_id: Job UUID to link

        Returns:
            True if updated, False if session not found
        """
        try:
            session_uuid = UUID(session_id)
            job_uuid = UUID(job_id)
        except ValueError:
            return False

        async with self.acquire() as conn:
            result = await conn.execute(
                "UPDATE builder_sessions SET job_id = $1 WHERE id = $2",
                job_uuid,
                session_uuid,
            )

        return result == "UPDATE 1"

    async def update_builder_session_summary(
        self, session_id: str, summary: str
    ) -> bool:
        """Update the auto-summary for a builder session.

        Args:
            session_id: Session UUID
            summary: Compressed summary of older messages

        Returns:
            True if updated, False if session not found
        """
        try:
            uuid_val = UUID(session_id)
        except ValueError:
            return False

        async with self.acquire() as conn:
            result = await conn.execute(
                "UPDATE builder_sessions SET summary = $1 WHERE id = $2",
                summary,
                uuid_val,
            )

        return result == "UPDATE 1"

    async def create_builder_message(
        self,
        session_id: str,
        role: str,
        content: str | None = None,
        tool_calls: List[Dict[str, Any]] | None = None,
        steps: List[Dict[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        """Create a new message in a builder session.

        Args:
            session_id: Session UUID
            role: Message role ('user' or 'assistant')
            content: Conversational text content
            tool_calls: List of artifact mutations (assistant only)
            steps: Agent reasoning steps (assistant only)

        Returns:
            Created message dict
        """
        session_uuid = UUID(session_id)

        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO builder_messages (session_id, role, content, tool_calls, steps)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, session_id, role, content, tool_calls, steps, created_at
                """,
                session_uuid,
                role,
                content,
                json.dumps(tool_calls) if tool_calls else None,
                json.dumps(steps) if steps else None,
            )

        return dict(row)

    async def get_builder_messages(
        self,
        session_id: str,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Get messages for a builder session in chronological order.

        Args:
            session_id: Session UUID
            limit: Maximum messages to return

        Returns:
            List of message dicts ordered by created_at ASC
        """
        try:
            uuid_val = UUID(session_id)
        except ValueError:
            return []

        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, session_id, role, content, tool_calls, steps, created_at
                FROM builder_messages
                WHERE session_id = $1
                ORDER BY created_at ASC
                LIMIT $2
                """,
                uuid_val,
                limit,
            )

        results = [dict(row) for row in rows]
        for msg in results:
            for key in ("tool_calls", "steps"):
                val = msg.get(key)
                if isinstance(val, str):
                    msg[key] = json.loads(val)
        return results

    # =========================================================================
    # SCHEMA MANAGEMENT
    # =========================================================================

    async def create_database_if_not_exists(self) -> bool:
        """Create the database if it doesn't exist.

        Connects to the 'postgres' database to check/create the target database.

        Returns:
            True if database was created, False if it already existed.

        Raises:
            RuntimeError: If database name cannot be extracted from connection string.
        """
        # Extract database name from connection string
        # Format: postgresql://user:pass@host:port/dbname
        db_name = self._connection_string.rsplit("/", 1)[-1].split("?")[0]
        if not db_name:
            raise RuntimeError("Could not extract database name from connection string")

        # Connect to postgres database to create the target database
        base_conn_str = self._connection_string.rsplit("/", 1)[0] + "/postgres"

        conn = await asyncpg.connect(base_conn_str)
        try:
            exists = await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", db_name
            )
            if not exists:
                # Use quoted identifier to handle special characters
                await conn.execute(f'CREATE DATABASE "{db_name}"')
                logger.info(f"Created database: {db_name}")
                return True
            logger.debug(f"Database already exists: {db_name}")
            return False
        finally:
            await conn.close()

    async def ensure_schema(self, schema_file: Path | None = None) -> bool:
        """Apply a schema file to initialize database tables.

        This is idempotent - uses IF NOT EXISTS clauses.
        Requires an active connection pool (call connect() first).

        Args:
            schema_file: Path to the SQL schema file. Defaults to schema.sql
                         (app DB). Pass vector_schema.sql for the vector DB.

        Returns:
            True if schema was applied successfully.

        Raises:
            RuntimeError: If not connected to database.
            FileNotFoundError: If schema file doesn't exist.
        """
        target = schema_file or SCHEMA_FILE
        if not target.exists():
            raise FileNotFoundError(f"Schema file not found: {target}")

        schema_sql = target.read_text()

        async with self.acquire() as conn:
            await conn.execute(schema_sql)

        logger.info(f"Applied schema from {target}")

        # Migration only applies to the app DB schema
        if target == SCHEMA_FILE:
            await self.migrate_existing_users_verified()

        return True

    async def reset_schema(self) -> None:
        """Drop all tables and recreate schema.

        WARNING: This deletes all data!

        Drops the public schema entirely and recreates it,
        then applies schema.sql.

        Raises:
            RuntimeError: If not connected to database.
        """
        async with self.acquire() as conn:
            # Nuclear option: drop and recreate public schema
            await conn.execute("DROP SCHEMA public CASCADE")
            await conn.execute("CREATE SCHEMA public")
            await conn.execute("GRANT ALL ON SCHEMA public TO public")
            logger.info("Dropped all tables (schema reset)")

        # Apply fresh schema
        await self.ensure_schema()

    async def verify_schema(self) -> Dict[str, bool]:
        """Verify all required tables exist.

        Returns:
            Dict mapping table names to existence status.

        Raises:
            RuntimeError: If not connected to database.
        """
        result = {}
        async with self.acquire() as conn:
            for table in REQUIRED_TABLES:
                exists = await conn.fetchval(
                    "SELECT EXISTS (SELECT FROM information_schema.tables "
                    "WHERE table_name = $1 AND table_schema = 'public')",
                    table,
                )
                result[table] = bool(exists)
                logger.debug(f"Table {table}: {'exists' if exists else 'MISSING'}")

        # Log summary
        missing = [t for t, exists in result.items() if not exists]
        if missing:
            logger.warning(f"Missing tables: {', '.join(missing)}")
        else:
            logger.info(f"All {len(REQUIRED_TABLES)} required tables exist")

        return result

    # =========================================================================
    # MESSAGE LOG (Agent-Human Communication)
    # =========================================================================

    async def log_message(
        self,
        job_id: str,
        thread_id: str,
        direction: str,
        subject: str,
        message: str,
        status: str,
        user_id: str | None = None,
        recipient_email: str | None = None,
        mode: str | None = None,
        error_message: str | None = None,
        email_message_id: str | None = None,
    ) -> Dict[str, Any] | None:
        """Log a message to the message_log table.

        Args:
            job_id: Job UUID
            thread_id: Short thread identifier
            direction: 'outbound' or 'inbound'
            subject: Message subject
            message: Message body
            status: 'sent', 'failed', 'rate_limited', 'delivered'
            user_id: Optional user UUID
            recipient_email: Recipient email address
            mode: 'async' or 'blocking' (outbound only)
            error_message: Error details if failed
            email_message_id: RFC822 Message-ID for IMAP reply correlation

        Returns:
            Created message_log row as dict, or None on failure
        """
        try:
            job_uuid = UUID(job_id)
        except ValueError:
            return None

        user_uuid = None
        if user_id:
            try:
                user_uuid = UUID(user_id)
            except ValueError:
                pass

        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO message_log (
                    job_id, user_id, thread_id, direction, recipient_email,
                    subject, message, mode, status, error_message, email_message_id
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                RETURNING id, job_id, thread_id, direction, status, created_at
                """,
                job_uuid,
                user_uuid,
                thread_id,
                direction,
                recipient_email,
                subject,
                message,
                mode,
                status,
                error_message,
                email_message_id,
            )

        return dict(row) if row else None

    async def message_exists_by_email_id(self, email_message_id: str) -> bool:
        """Check if a message with this RFC822 Message-ID already exists.

        Used by the IMAP poller for deduplication.
        """
        async with self.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM message_log WHERE email_message_id = $1",
                email_message_id,
            )
        return (count or 0) > 0

    async def get_thread_email_message_id(
        self, job_id: str, thread_id: str
    ) -> str | None:
        """Get the most recent outbound Message-ID for building In-Reply-To headers."""
        try:
            job_uuid = UUID(job_id)
        except ValueError:
            return None

        async with self.acquire() as conn:
            return await conn.fetchval(
                """
                SELECT email_message_id FROM message_log
                WHERE job_id = $1 AND thread_id = $2 AND direction = 'outbound'
                      AND email_message_id IS NOT NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                job_uuid,
                thread_id,
            )

    async def get_job_by_short_id(self, short_id: str) -> Dict[str, Any] | None:
        """Look up a job by the first 8 characters of its UUID.

        Used by the IMAP poller to resolve job from + sub-address.
        """
        if not short_id or len(short_id) < 4:
            return None

        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM jobs WHERE id::text LIKE $1 || '%' LIMIT 1",
                short_id,
            )
        return dict(row) if row else None

    async def resolve_message_by_email_id(
        self, email_message_id: str
    ) -> Dict[str, Any] | None:
        """Look up a message_log entry by its RFC822 Message-ID.

        Used by the IMAP poller to resolve In-Reply-To headers when
        + sub-addressing is stripped by the email client.

        Returns:
            Dict with job_id (str) and thread_id, or None.
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT job_id::text AS job_id, thread_id
                FROM message_log
                WHERE email_message_id = $1
                LIMIT 1
                """,
                email_message_id,
            )
        return dict(row) if row else None

    async def check_message_rate_limit(
        self,
        job_id: str,
        user_id: str | None = None,
    ) -> Dict[str, int]:
        """Check message rate limits for a job and user.

        Args:
            job_id: Job UUID
            user_id: Optional user UUID for per-user limit check

        Returns:
            Dict with 'job_hourly', 'job_daily', 'user_daily' counts
        """
        try:
            job_uuid = UUID(job_id)
        except ValueError:
            return {"job_hourly": 0, "job_daily": 0, "user_daily": 0}

        async with self.acquire() as conn:
            job_hourly = await conn.fetchval(
                """
                SELECT COUNT(*) FROM message_log
                WHERE job_id = $1
                  AND direction = 'outbound'
                  AND status != 'rate_limited'
                  AND created_at > NOW() - INTERVAL '1 hour'
                """,
                job_uuid,
            )

            job_daily = await conn.fetchval(
                """
                SELECT COUNT(*) FROM message_log
                WHERE job_id = $1
                  AND direction = 'outbound'
                  AND status != 'rate_limited'
                  AND created_at > NOW() - INTERVAL '24 hours'
                """,
                job_uuid,
            )

            user_daily = 0
            if user_id:
                try:
                    user_uuid = UUID(user_id)
                    user_daily = await conn.fetchval(
                        """
                        SELECT COUNT(*) FROM message_log
                        WHERE user_id = $1
                          AND direction = 'outbound'
                          AND status != 'rate_limited'
                          AND created_at > NOW() - INTERVAL '24 hours'
                        """,
                        user_uuid,
                    )
                except ValueError:
                    pass

        return {
            "job_hourly": job_hourly or 0,
            "job_daily": job_daily or 0,
            "user_daily": user_daily or 0,
        }

    async def get_message_threads(self, job_id: str) -> List[Dict[str, Any]]:
        """Get message threads for a job.

        Args:
            job_id: Job UUID

        Returns:
            List of thread summary dicts with thread_id, subject, message_count, etc.
        """
        try:
            job_uuid = UUID(job_id)
        except ValueError:
            return []

        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    thread_id,
                    MIN(subject) AS subject,
                    COUNT(*) AS message_count,
                    MAX(created_at) AS last_message_at,
                    COUNT(*) FILTER (WHERE direction = 'outbound') AS sent_count,
                    COUNT(*) FILTER (WHERE direction = 'inbound') AS received_count,
                    MIN(mode) FILTER (WHERE direction = 'outbound') AS mode,
                    MIN(created_at) AS started_at
                FROM message_log
                WHERE job_id = $1
                GROUP BY thread_id
                ORDER BY MAX(created_at) DESC
                """,
                job_uuid,
            )

        return [dict(row) for row in rows]

    async def get_thread_messages(
        self, job_id: str, thread_id: str
    ) -> Dict[str, Any] | None:
        """Get full ordered messages within a thread.

        Args:
            job_id: Job UUID
            thread_id: Thread identifier

        Returns:
            Dict with thread metadata and ordered messages, or None if empty.
        """
        try:
            job_uuid = UUID(job_id)
        except ValueError:
            return None

        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    id, direction, subject, message, mode,
                    read_at, created_at
                FROM message_log
                WHERE job_id = $1 AND thread_id = $2
                ORDER BY created_at ASC
                """,
                job_uuid,
                thread_id,
            )

        if not rows:
            return None

        messages = []
        for row in rows:
            messages.append(
                {
                    "id": str(row["id"]),
                    "direction": row["direction"],
                    "subject": row["subject"],
                    "message": row["message"],
                    "created_at": row["created_at"].isoformat()
                    if row["created_at"]
                    else None,
                    "read_at": row["read_at"].isoformat() if row["read_at"] else None,
                }
            )

        # Thread metadata from first outbound message
        first_outbound = next(
            (r for r in rows if r["direction"] == "outbound"), rows[0]
        )

        return {
            "thread_id": thread_id,
            "subject": first_outbound["subject"],
            "mode": first_outbound.get("mode") or "async",
            "messages": messages,
        }

    async def get_pending_action_counts(self) -> Dict[str, Any]:
        """Get counts of pending actions across all types.

        Returns:
            Dict with sudo, messages, reviews counts and most_urgent info.
        """
        async with self.acquire() as conn:
            sudo_count = (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM sudo_approval_requests WHERE status = 'pending'"
                )
                or 0
            )

            message_count = (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM jobs WHERE status = 'waiting_for_reply'"
                )
                or 0
            )

            review_count = (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM jobs WHERE status = 'pending_review'"
                )
                or 0
            )

            # Find most urgent sudo request (lowest TTL)
            most_urgent_sudo = await conn.fetchrow(
                """
                SELECT id, command, expires_at
                FROM sudo_approval_requests
                WHERE status = 'pending' AND expires_at > NOW()
                ORDER BY expires_at ASC
                LIMIT 1
                """
            )

        total = sudo_count + message_count + review_count

        most_urgent = None
        if most_urgent_sudo:
            expires_at = most_urgent_sudo["expires_at"]
            expires_in = (expires_at - datetime.now(timezone.utc)).total_seconds()
            if expires_in > 0:
                most_urgent = {
                    "type": "sudo",
                    "id": str(most_urgent_sudo["id"]),
                    "title": most_urgent_sudo["command"],
                    "expires_in_seconds": int(expires_in),
                }

        return {
            "counts": {
                "sudo": sudo_count,
                "messages": message_count,
                "reviews": review_count,
                "total": total,
            },
            "most_urgent": most_urgent,
        }

    async def get_message_sequence(self, job_id: str, thread_id: str) -> int:
        """Get the next sequence number for a thread.

        Args:
            job_id: Job UUID
            thread_id: Thread identifier

        Returns:
            Next sequence number (1-based)
        """
        try:
            job_uuid = UUID(job_id)
        except ValueError:
            return 1

        async with self.acquire() as conn:
            count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM message_log
                WHERE job_id = $1 AND thread_id = $2
                """,
                job_uuid,
                thread_id,
            )

        return (count or 0) + 1

    # =========================================================================
    # External Contacts (Phase 3 Live Communication)
    # =========================================================================

    async def add_external_contact(
        self,
        project_id: str,
        display_name: str,
        email: str,
        added_by: str | None = None,
    ) -> Dict[str, Any]:
        """Add an external contact to a project.

        Args:
            project_id: Project UUID
            display_name: Contact display name
            email: Contact email address
            added_by: User UUID who added the contact

        Returns:
            Created contact record.
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO external_contacts (project_id, display_name, email, added_by)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (project_id, email) DO UPDATE
                    SET display_name = EXCLUDED.display_name
                RETURNING id, project_id, display_name, email, added_by, created_at
                """,
                UUID(project_id),
                display_name,
                email,
                UUID(added_by) if added_by else None,
            )
        return dict(row)

    async def get_external_contacts(self, project_id: str) -> List[Dict[str, Any]]:
        """Get all external contacts for a project."""
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, project_id, display_name, email, added_by, created_at
                FROM external_contacts
                WHERE project_id = $1
                ORDER BY display_name
                """,
                UUID(project_id),
            )
        return [dict(r) for r in rows]

    async def delete_external_contact(self, contact_id: str) -> bool:
        """Delete an external contact by ID. Returns True if deleted."""
        async with self.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM external_contacts WHERE id = $1",
                UUID(contact_id),
            )
        return result == "DELETE 1"

    async def resolve_external_contact(
        self,
        project_id: str,
        name_or_email: str,
    ) -> Dict[str, Any] | None:
        """Resolve an external contact by display name or email (case-insensitive).

        Args:
            project_id: Project UUID
            name_or_email: Display name or email to match

        Returns:
            Contact dict or None if not found.
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, project_id, display_name, email, added_by, created_at
                FROM external_contacts
                WHERE project_id = $1
                    AND (LOWER(display_name) = LOWER($2) OR LOWER(email) = LOWER($2))
                LIMIT 1
                """,
                UUID(project_id),
                name_or_email,
            )
        return dict(row) if row else None

    # =========================================================================
    # Notification Queue (Phase 3 Live Communication)
    # =========================================================================

    async def queue_notification(
        self,
        user_id: str,
        job_id: str,
        thread_id: str | None,
        subject: str,
        message: str,
        channels: dict,
    ) -> Dict[str, Any]:
        """Queue a notification for later digest delivery (quiet hours)."""
        import json as _json

        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO notification_queue
                    (user_id, job_id, thread_id, subject, message, channels)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                RETURNING id, user_id, job_id, thread_id, subject, queued_at
                """,
                UUID(user_id),
                UUID(job_id) if job_id else None,
                thread_id,
                subject,
                message,
                _json.dumps(channels),
            )
        return dict(row)

    async def get_pending_notifications(self, user_id: str) -> List[Dict[str, Any]]:
        """Get undelivered notifications for a user."""
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, user_id, job_id, thread_id, subject, message, channels,
                       queued_at
                FROM notification_queue
                WHERE user_id = $1 AND delivered_at IS NULL
                ORDER BY queued_at
                """,
                UUID(user_id),
            )
        return [dict(r) for r in rows]

    async def mark_notifications_delivered(self, ids: List[str]) -> int:
        """Mark notifications as delivered. Returns count updated."""
        if not ids:
            return 0
        uuids = [UUID(i) for i in ids]
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE notification_queue
                SET delivered_at = NOW()
                WHERE id = ANY($1::uuid[])
                """,
                uuids,
            )
        # result is like "UPDATE N"
        return int(result.split()[-1]) if result else 0

    async def get_users_exiting_quiet_hours(
        self,
        check_window_minutes: int = 5,
    ) -> List[Dict[str, Any]]:
        """Find users whose quiet hours ended within the check window
        and who have pending notifications.

        Returns list of dicts with user_id and settings.
        """
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT u.id AS user_id, u.settings
                FROM users u
                JOIN notification_queue nq ON nq.user_id = u.id AND nq.delivered_at IS NULL
                WHERE u.settings->'communication'->'quiet_hours'->>'enabled' = 'true'
                """,
            )
        return [dict(r) for r in rows]

    # =========================================================================
    # Notification Read Tracking (Phase 3)
    # =========================================================================

    async def get_user_notifications(
        self,
        user_id: str,
        limit: int = 50,
        unread_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """Get notifications (outbound messages) for a user."""
        where = "WHERE ml.user_id = $1 AND ml.direction = 'outbound'"
        if unread_only:
            where += " AND ml.read_at IS NULL"

        async with self.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT ml.id, ml.job_id, ml.thread_id, ml.subject, ml.message,
                       ml.status, ml.created_at, ml.read_at,
                       j.description AS job_description, j.config_name
                FROM message_log ml
                LEFT JOIN jobs j ON j.id = ml.job_id
                {where}
                ORDER BY ml.created_at DESC
                LIMIT $2
                """,
                UUID(user_id),
                limit,
            )
        return [dict(r) for r in rows]

    async def mark_notification_read(self, message_id: str, user_id: str) -> bool:
        """Mark a notification as read. Returns True if updated."""
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE message_log SET read_at = NOW()
                WHERE id = $1 AND user_id = $2 AND read_at IS NULL
                """,
                UUID(message_id),
                UUID(user_id),
            )
        return result == "UPDATE 1"

    async def get_unread_count(self, user_id: str) -> int:
        """Count unread notifications for a user."""
        async with self.acquire() as conn:
            count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM message_log
                WHERE user_id = $1 AND direction = 'outbound' AND read_at IS NULL
                """,
                UUID(user_id),
            )
        return count or 0

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


__all__ = [
    "PostgresDB",
    "ALLOWED_TABLES",
    "PG_TYPE_MAP",
    "SCHEMA_FILE",
    "REQUIRED_TABLES",
]
