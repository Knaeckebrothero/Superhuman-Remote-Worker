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

from security.crypto import (
    DecryptionError,
    decrypt,
    encrypt,
    is_encrypted,
)

from utils.db_url import build_postgres_url

logger = logging.getLogger(__name__)


def _encrypt_optional(value: str | None) -> str | None:
    """Encrypt a credential before storage. NULL stays NULL; empty is treated as NULL."""
    if value is None or value == "":
        return None
    return encrypt(value)


def _decrypt_stored(value: str | None, *, field: str) -> str | None:
    """Decrypt a credential after fetch. NULL stays NULL.

    If the value is not a v1 ciphertext (e.g. pre-encryption dev data), the
    row is unusable — return None and log rather than crash the caller. Pre-
    production upgrade path: operators re-add keys through the UI.
    """
    if value is None:
        return None
    if not is_encrypted(value):
        logger.warning(
            "Encountered non-encrypted value in %s; ignoring. "
            "Re-add the credential via the UI.",
            field,
        )
        return None
    try:
        return decrypt(value)
    except DecryptionError as exc:
        logger.error("Failed to decrypt %s: %s", field, exc)
        return None


QUERIES_DIR = Path(__file__).parent / "queries" / "postgres"

# Frozen schema reference (no longer applied at runtime — see migrate.py).
SCHEMA_FILE = Path(__file__).parent / "schema.sql"

# Migration directories per DB. The runner picks one of these via the
# ``migrations_dir`` constructor kwarg on ``PostgresDB``; lifespan + init
# wire each instance to its own subdir.
MIGRATIONS_APP_DIR = Path(__file__).parent / "migrations" / "app"
MIGRATIONS_VECTOR_DIR = Path(__file__).parent / "migrations" / "vector"

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
    "models",
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
        migrations_dir: Optional[Path] = None,
    ):
        """Initialize PostgreSQL database manager.

        Args:
            connection_string: PostgreSQL connection URL. Falls back to DATABASE_URL env var.
            min_connections: Minimum pool size (default: 2)
            max_connections: Maximum pool size (default: 10)
            command_timeout: Query timeout in seconds (default: 60.0)
            migrations_dir: Directory of NNNN_*.sql migrations applied by
                ``apply_migrations``. Defaults to ``migrations/app/``; the
                vector instance overrides this to ``migrations/vector/``.

        Raises:
            ImportError: If asyncpg is not installed
            ValueError: If no connection string provided
        """
        if asyncpg is None:
            raise ImportError(
                "asyncpg is required for PostgreSQL support. "
                "Install it with: pip install asyncpg"
            )

        self._connection_string = (
            connection_string
            or build_postgres_url(
                "POSTGRES",
                fallback_env="DATABASE_URL",
            )
            or "postgresql://srw:srw_password@localhost:5432/srw"
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
        self._migrations_dir: Path = migrations_dir or MIGRATIONS_APP_DIR

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
        *,
        scope_project_id: str | None = None,
    ) -> List[Dict[str, Any]]:
        """Get list of jobs with optional status, owner, and scope filters.

        AND-style filtering — used by the admin path (full fleet view, with
        optional ``?user_id=`` and/or MCP ``project:<uuid>`` scope narrowing).
        Non-admin callers must go through :meth:`get_visible_jobs` instead so
        the visibility OR-clause is applied.

        Args:
            status: Optional status filter (e.g., 'completed', 'processing')
            user_id: Optional user ID filter (admin cross-user query)
            limit: Maximum number of jobs to return
            scope_project_id: MCP token ``project:<uuid>`` narrowing. When
                set, an additional ``project_id = $scope`` filter is appended.

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

        if scope_project_id:
            param_count += 1
            conditions.append(f"project_id = ${param_count}")
            values.append(UUID(scope_project_id))

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

    async def get_visible_jobs(
        self,
        *,
        owner_user_id: str,
        visible_project_ids: list[str],
        status: str | None = None,
        scope_project_id: str | None = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Get list of jobs visible to a non-admin caller (G1 visibility OR).

        Applies the user-visibility model: ``(user_id = $owner OR
        project_id = ANY($projects))``. ``owner_user_id`` is the caller's own
        id; ``visible_project_ids`` is the list of project ids the caller is a
        member of (from
        :func:`security.access.user_visible_project_ids`). An empty
        ``visible_project_ids`` is fine — the OR-clause then just restricts to
        the caller's own jobs.

        ``scope_project_id`` is the MCP token's ``project:<uuid>`` narrowing
        (if any); AND-combined on top, which intersects the visibility set
        down to that one project.

        Admin callers must NOT use this helper — they go through
        :meth:`get_jobs` so the OR-clause isn't applied (admins see the full
        fleet, possibly with explicit ``?user_id=`` or scope filters).
        """
        conditions: list[str] = []
        values: list[Any] = []
        param_count = 0

        if status:
            param_count += 1
            conditions.append(f"status = ${param_count}")
            values.append(status)

        param_count += 1
        user_idx = param_count
        param_count += 1
        projects_idx = param_count
        conditions.append(
            f"(user_id = ${user_idx} OR project_id = ANY(${projects_idx}::uuid[]))"
        )
        values.append(UUID(owner_user_id))
        values.append([UUID(p) for p in visible_project_ids])

        if scope_project_id:
            param_count += 1
            conditions.append(f"project_id = ${param_count}")
            values.append(UUID(scope_project_id))

        where_clause = f"WHERE {' AND '.join(conditions)}"
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

    async def get_descendant_jobs(self, job_id: str) -> List[Dict[str, Any]]:
        """Get all non-terminal descendant jobs (recursive).

        Walks the parent_job_id tree downward and returns every descendant
        whose status is not yet terminal (completed/failed/cancelled).
        Includes all subjob types: scholar, critic, curator, delegation.

        Args:
            job_id: Root job UUID as string

        Returns:
            List of job dicts for active descendants (may be empty)
        """
        try:
            uuid_val = UUID(job_id)
        except ValueError:
            return []

        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH RECURSIVE descendants AS (
                    SELECT id, parent_job_id, status, assigned_agent_id,
                           config_name, context, 0 AS depth
                    FROM jobs
                    WHERE parent_job_id = $1

                    UNION ALL

                    SELECT j.id, j.parent_job_id, j.status, j.assigned_agent_id,
                           j.config_name, j.context, d.depth + 1
                    FROM jobs j
                    JOIN descendants d ON j.parent_job_id = d.id
                    WHERE d.depth < 20
                )
                SELECT *
                FROM descendants
                WHERE status NOT IN ('completed', 'failed', 'cancelled')
                """,
                uuid_val,
            )

        return [dict(row) for row in rows]

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

    async def merge_thread_config_override(
        self, thread_id: str, config_updates: Dict[str, Any]
    ) -> bool:
        """Deep-merge updates into threads.metadata.config_override.

        Unlike the shallow ``||`` merge used by workspace/vm/snapshot helpers,
        this method reads the current config_override, performs a recursive
        Python-side merge, and writes the result back.  This allows nested
        keys (e.g. ``llm.model`` and ``llm.temperature``) to be updated
        independently without clobbering each other.

        Args:
            thread_id: Thread UUID as string
            config_updates: Partial config dict to merge
                            (e.g. ``{"llm": {"model": "..."}}``).

        Returns:
            True if updated, False if thread not found
        """
        import json as json_module

        try:
            uuid_val = UUID(thread_id)
        except ValueError:
            return False

        def _deep_merge(base: Dict, override: Dict) -> Dict:
            merged = dict(base)
            for key, value in override.items():
                if (
                    key in merged
                    and isinstance(merged[key], dict)
                    and isinstance(value, dict)
                ):
                    merged[key] = _deep_merge(merged[key], value)
                else:
                    merged[key] = value
            return merged

        async with self.acquire() as conn:
            # Read current config_override
            row = await conn.fetchrow(
                "SELECT metadata FROM threads WHERE id = $1", uuid_val
            )
            if not row:
                return False

            metadata = row["metadata"] or {}
            if isinstance(metadata, str):
                try:
                    metadata = json_module.loads(metadata)
                except (json_module.JSONDecodeError, TypeError):
                    metadata = {}

            current = metadata.get("config_override") or {}
            merged = _deep_merge(current, config_updates)

            result = await conn.execute(
                "UPDATE threads "
                "SET metadata = jsonb_set("
                "    COALESCE(metadata, '{}'::jsonb), "
                "    '{config_override}', "
                "    $1::jsonb"
                "), "
                "    last_activity = CURRENT_TIMESTAMP "
                "WHERE id = $2",
                json_module.dumps(merged),
                uuid_val,
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

    def _visibility_clause(
        self,
        *,
        owner_user_id: str | None,
        visible_project_ids: list[str] | None,
        scope_project_id: str | None,
        table_alias: str = "",
        start_idx: int = 1,
    ) -> tuple[str, list[Any], int]:
        """Build the G1/G5 visibility WHERE fragment for the jobs table.

        Returns (sql_fragment, values, next_idx). When all visibility
        params are None, returns empty fragment (admin view). When
        ``owner_user_id`` is set, emits the OR-clause
        ``(user_id = $u OR project_id = ANY($p))``. ``scope_project_id``
        is AND-combined on top to honour MCP token narrowing.

        ``table_alias`` is the SQL alias for the jobs table (e.g. ``"j"``).
        Empty string means no alias (raw column names).
        """
        prefix = f"{table_alias}." if table_alias else ""
        values: list[Any] = []
        idx = start_idx
        conditions: list[str] = []

        if owner_user_id is not None:
            user_idx = idx
            idx += 1
            projects_idx = idx
            idx += 1
            conditions.append(
                f"({prefix}user_id = ${user_idx} "
                f"OR {prefix}project_id = ANY(${projects_idx}::uuid[]))"
            )
            values.append(UUID(owner_user_id))
            values.append([UUID(p) for p in (visible_project_ids or [])])

        if scope_project_id is not None:
            conditions.append(f"{prefix}project_id = ${idx}")
            values.append(UUID(scope_project_id))
            idx += 1

        fragment = " AND ".join(conditions)
        return fragment, values, idx

    async def get_daily_statistics(
        self,
        days: int = 7,
        *,
        owner_user_id: str | None = None,
        visible_project_ids: list[str] | None = None,
        scope_project_id: str | None = None,
    ) -> List[Dict[str, Any]]:
        """Get daily job statistics for the past N days.

        Args:
            days: Number of days to include
            owner_user_id: G5 visibility — when set, the result is restricted
                via the OR-clause ``(user_id = $owner OR project_id ANY
                $visible_project_ids)``. Admins pass ``None`` to see all.
            visible_project_ids: caller's project memberships (only consulted
                when ``owner_user_id`` is set).
            scope_project_id: MCP ``project:<uuid>`` narrowing — AND-combined
                on top.

        Returns:
            List of daily statistics dictionaries
        """
        visibility, vis_vals, next_idx = self._visibility_clause(
            owner_user_id=owner_user_id,
            visible_project_ids=visible_project_ids,
            scope_project_id=scope_project_id,
            start_idx=2,  # $1 is days
        )
        where_extra = f" AND {visibility}" if visibility else ""
        async with self.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT
                    DATE(created_at) as date,
                    COUNT(*) as jobs_created,
                    COUNT(*) FILTER (WHERE status = 'completed') as jobs_completed,
                    COUNT(*) FILTER (WHERE status = 'failed') as jobs_failed,
                    COUNT(*) FILTER (WHERE status = 'cancelled') as jobs_cancelled
                FROM jobs
                WHERE created_at > CURRENT_TIMESTAMP - INTERVAL '1 day' * $1
                {where_extra}
                GROUP BY DATE(created_at)
                ORDER BY date DESC
                """,
                days,
                *vis_vals,
            )

        return [dict(row) for row in rows]

    async def get_job_statistics(
        self,
        *,
        owner_user_id: str | None = None,
        visible_project_ids: list[str] | None = None,
        scope_project_id: str | None = None,
    ) -> Dict[str, int]:
        """Get overall job statistics.

        Args:
            owner_user_id / visible_project_ids / scope_project_id:
                G5 visibility — see :meth:`get_daily_statistics` for the
                semantics. ``None`` on all three = full fleet (admin view).

        Returns:
            Dict with job counts by status
        """
        visibility, vis_vals, _next_idx = self._visibility_clause(
            owner_user_id=owner_user_id,
            visible_project_ids=visible_project_ids,
            scope_project_id=scope_project_id,
            start_idx=1,
        )
        where_clause = f"WHERE {visibility}" if visibility else ""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT
                    COUNT(*) as total_jobs,
                    COUNT(*) FILTER (WHERE status = 'created') as created,
                    COUNT(*) FILTER (WHERE status = 'processing') as processing,
                    COUNT(*) FILTER (WHERE status = 'completed') as completed,
                    COUNT(*) FILTER (WHERE status = 'failed') as failed,
                    COUNT(*) FILTER (WHERE status = 'cancelled') as cancelled
                FROM jobs
                {where_clause}
                """,
                *vis_vals,
            )

        return dict(row) if row else {}

    async def detect_stuck_jobs(
        self,
        threshold_minutes: int = 60,
        *,
        owner_user_id: str | None = None,
        visible_project_ids: list[str] | None = None,
        scope_project_id: str | None = None,
    ) -> List[Dict[str, Any]]:
        """Detect jobs that appear to be stuck.

        A job is considered stuck if it's in 'processing' status but hasn't
        been updated within the threshold period.

        Args:
            threshold_minutes: Minutes without activity to consider stuck
            owner_user_id / visible_project_ids / scope_project_id:
                G5 visibility — see :meth:`get_daily_statistics`.

        Returns:
            List of stuck job dictionaries with stuck reason
        """
        threshold = datetime.now(timezone.utc) - timedelta(minutes=threshold_minutes)

        visibility, vis_vals, _next_idx = self._visibility_clause(
            owner_user_id=owner_user_id,
            visible_project_ids=visible_project_ids,
            scope_project_id=scope_project_id,
            table_alias="j",
            start_idx=2,  # $1 is threshold
        )
        where_extra = f" AND {visibility}" if visibility else ""

        async with self.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT j.id, j.description, j.status,
                       j.config_name, j.assigned_agent_id, j.created_at, j.updated_at
                FROM jobs j
                WHERE j.status = 'processing'
                AND j.updated_at < $1
                {where_extra}
                ORDER BY j.updated_at ASC
                """,
                threshold,
                *vis_vals,
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
        build_sha: str | None = None,
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
                            thread_id     = $7,
                            metadata = COALESCE(metadata, '{}') || $8::jsonb
                        WHERE id = $5
                        """,
                        pod_ip,
                        pod_port,
                        pid,
                        config_name,
                        agent_id,
                        agent_mode,
                        thread_id,
                        json.dumps({"build_sha": build_sha or ""}),
                    )
                    return {
                        "agent_id": str(agent_id),
                        "heartbeat_interval_seconds": 60,
                    }

            # Create new agent
            row = await conn.fetchrow(
                """
                INSERT INTO agents (config_name, hostname, pod_ip, pod_port, pid, agent_mode, thread_id, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
                RETURNING id
                """,
                config_name,
                hostname,
                pod_ip,
                pod_port,
                pid,
                agent_mode,
                thread_id,
                json.dumps({"build_sha": build_sha or ""}),
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
            # Fetch previous status (transition detection) + intents
            # (orchestrator-set drain/upgrade hints that the agent reads
            # from the heartbeat response and reacts to).
            prev = await conn.fetchrow(
                "SELECT status, intents FROM agents WHERE id = $1",
                uuid_val,
            )
            if not prev:
                return None

            prev_status = prev["status"]
            intents_raw = prev["intents"] if "intents" in prev else None
            if isinstance(intents_raw, str):
                try:
                    intents = json.loads(intents_raw)
                except (json.JSONDecodeError, ValueError):
                    intents = {}
            elif isinstance(intents_raw, dict):
                intents = intents_raw
            else:
                intents = {}

            # Set last_completed_at when transitioning from working → ready/completed
            # This enables the dispatch cooldown (30s before next job assignment)
            set_completed = prev_status == "working" and status in (
                "ready",
                "completed",
            )

            # Phase 0 stopgap: an orchestrator-set 'draining' status is
            # preserved against the agent's reported status. The agent's
            # heartbeat would otherwise overwrite drain intent on the next
            # 5s tick. Phase 1 replaces this with a separate intent column.
            if metrics:
                result = await conn.execute(
                    f"""
                    UPDATE agents
                    SET status = CASE WHEN status = 'draining' THEN 'draining' ELSE $1 END,
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
                    SET status = CASE WHEN status = 'draining' THEN 'draining' ELSE $1 END,
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

            effective_status = "draining" if prev_status == "draining" else status
            return {
                "previous_status": prev_status,
                "effective_status": effective_status,
                "intents": intents,
            }

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
                       status, current_job_id, thread_id,
                       registered_at, last_heartbeat, metadata
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
        that have no assigned agent.  Excludes jobs whose ancestor chain
        contains a paused, cancelled, or failed parent (cascade guard).

        Args:
            limit: Maximum jobs to return

        Returns:
            List of job dicts ordered by priority DESC, created_at ASC
        """
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT j.id, j.description, j.status, j.config_name,
                       j.config_override, j.assigned_agent_id, j.user_id,
                       j.project_id, j.parent_job_id, j.priority,
                       j.branch_name, j.context, j.created_at
                FROM jobs j
                WHERE j.status IN ('created', 'paused')
                  AND j.assigned_agent_id IS NULL
                  AND j.freeze_data IS NULL
                  AND NOT EXISTS (
                      WITH RECURSIVE ancestors AS (
                          SELECT parent_job_id
                          FROM jobs
                          WHERE id = j.id AND parent_job_id IS NOT NULL

                          UNION ALL

                          SELECT p.parent_job_id
                          FROM jobs p
                          JOIN ancestors a ON p.id = a.parent_job_id
                          WHERE a.parent_job_id IS NOT NULL
                      )
                      SELECT 1
                      FROM ancestors a2
                      JOIN jobs blocked ON blocked.id = a2.parent_job_id
                      WHERE blocked.status IN ('paused', 'cancelled', 'failed')
                  )
                ORDER BY j.priority DESC, j.created_at ASC
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
                  AND COALESCE(agent_mode, 'worker') IN ('worker', 'dual')
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
        """Resume an ended thread — reset to 'created', clear stale agent."""
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

    async def update_thread_main_cloud(
        self,
        thread_id: str,
        *,
        backend_id: str,
        session_handle: str,
        share_handle: str | None = None,
    ) -> None:
        """Store main-cloud session folder info on a thread.

        Writes to both the new ``main_cloud_*`` columns and the legacy
        ``nc_session_folder`` / ``nc_share_id`` columns during Phase 1 so any
        code paths still reading the legacy columns keep working. The legacy
        columns are dropped one release after Phase 1 ships (see §9 of the
        design doc).
        """
        legacy_nc_folder = session_handle if backend_id == "nextcloud" else None
        legacy_share_id: int | None = None
        if backend_id == "nextcloud" and share_handle is not None:
            try:
                legacy_share_id = int(share_handle)
            except ValueError:
                legacy_share_id = None
        async with self.acquire() as conn:
            await conn.execute(
                """
                UPDATE threads
                SET main_cloud_backend        = $2,
                    main_cloud_session_handle = $3,
                    main_cloud_share_handle   = $4,
                    nc_session_folder         = $5,
                    nc_share_id               = $6
                WHERE id = $1
                """,
                UUID(thread_id),
                backend_id,
                session_handle,
                share_handle,
                legacy_nc_folder,
                legacy_share_id,
            )

    async def mark_orphaned_threads_ended(self) -> list[str]:
        """Mark threads as ended when their bound agent is offline.

        Only flags threads that have an ``agent_id`` pointing at an offline
        agent. ``agent_id IS NULL`` is intentionally excluded — it covers
        legitimate transient states (fresh thread awaiting first dispatch,
        thread post-Resume awaiting re-binding) that the dispatcher / WS
        proxy own. Catching those here would race the dispatcher and flip
        brand-new threads to 'ended' within a second of creation.

        Returns:
            List of thread IDs that were marked ended. The caller is
            responsible for tearing down the associated workspace + agent
            pods; this method only owns the DB state transition.
        """
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE threads
                SET status        = 'ended',
                    ended_at      = CURRENT_TIMESTAMP,
                    last_activity = CURRENT_TIMESTAMP
                WHERE status IN ('created', 'active')
                  AND agent_id IS NOT NULL
                  AND agent_id IN (SELECT id
                                   FROM agents
                                   WHERE status = 'offline')
                RETURNING id
                """
            )
        return [str(row["id"]) for row in rows]

    async def mark_stuck_working_agents_ready(self) -> int:
        """Reset agents whose self-reported status is internally inconsistent.

        An agent reporting ``status='working'`` while ``current_job_id IS NULL``
        is in a state that no normal lifecycle transition can produce — the
        heartbeat handler updates status and current_job_id atomically, so an
        agent without a job should be 'ready', not 'working'. Flip the row to
        'ready' so the dispatcher can use the slot.

        Defense in depth for ``_reset_to_idle()`` failures (heartbeat that
        never landed, exception during cleanup, agent process crash mid-
        transition). The ``mark_stale_agents_offline`` sweep doesn't catch
        these because the agents continue to heartbeat.

        Returns:
            Number of agents flipped to 'ready'.
        """
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE agents
                SET status = 'ready'
                WHERE status = 'working'
                  AND current_job_id IS NULL
                """
            )
        if result.startswith("UPDATE "):
            return int(result.split()[1])
        return 0

    async def mark_stuck_session_agents_ready(self) -> int:
        """Release agents still marked 'session' for threads that already ended.

        When a persistent thread transitions to 'ended' (idle timeout, manual
        end, orphan sweep) the agent is supposed to detach and either exit or
        loop back to 'ready'. If the detach didn't reach the orchestrator
        (agent process died before sending the post-detach heartbeat, agent
        bug, etc.) the agent row stays 'session' and the slot stays
        unavailable. This sweep flips it to 'ready' and clears thread_id.

        Grace of 2 minutes is on ``thread.ended_at`` — the *thread* must have
        been ended for at least that long before we intervene. Heartbeat
        freshness is intentionally NOT used because zombie agents heartbeat
        normally; gating on heartbeat would never let the sweep fire.

        Returns:
            Number of agents released.
        """
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE agents
                SET status    = 'ready',
                    thread_id = NULL
                WHERE status = 'session'
                  AND thread_id IS NOT NULL
                  AND thread_id IN (SELECT id
                                    FROM threads
                                    WHERE status = 'ended'
                                      AND ended_at < NOW() - INTERVAL '2 minutes')
                """
            )
        if result.startswith("UPDATE "):
            return int(result.split()[1])
        return 0

    async def gc_offline_agents(self, retention_hours: int = 24) -> int:
        """Delete agent rows that have been offline longer than the retention.

        Offline agents accumulate forever otherwise — `mark_stale_agents_offline`
        flips status but never removes the row. After ``retention_hours`` of
        no heartbeat the pod is definitely gone (k8s would have GC'd it long
        before), and keeping the row only bloats ``list_agents`` queries.

        FK behavior: ``threads.agent_id`` and ``jobs.assigned_agent_id`` are
        both ``ON DELETE SET NULL``, so deletion is safe.

        Args:
            retention_hours: How long offline agents are kept before deletion.

        Returns:
            Number of agent rows deleted.
        """
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM agents
                WHERE status = 'offline'
                  AND last_heartbeat < NOW() - ($1 || ' hours')::INTERVAL
                """,
                str(retention_hours),
            )
        if result.startswith("DELETE "):
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
        tool_call_id: Optional[str] = None,
        thinking: Optional[str] = None,
    ) -> str:
        """Save a message to thread_messages. Fire-and-forget safe.

        ``tool_call_id`` is set only on role='tool' rows and links the result
        back to the AIMessage's tool_calls[].id. ``thinking`` is set only on
        role='ai' rows that carry reasoning content. See migration 0011.
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO thread_messages
                    (thread_id, role, content, tool_calls, turn_number,
                     metrics, tool_call_id, thinking)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id
                """,
                thread_id,
                role,
                content,
                json.dumps(tool_calls) if tool_calls else None,
                turn_number,
                json.dumps(metrics) if metrics else None,
                tool_call_id,
                thinking,
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
                SELECT id, role, content, tool_calls, turn_number, metrics,
                       tool_call_id, thinking, created_at
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
                "tool_call_id": row["tool_call_id"],
                "thinking": row["thinking"],
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
                       cli_hint, default_branch, job_id, created_by, is_global,
                       created_at, updated_at
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
                       cli_hint, default_branch, job_id, created_by, is_global,
                       created_at, updated_at
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
        connection_url: str | None = None,
        description: str | None = None,
        credentials: Dict[str, Any] | None = None,
        job_id: str | None = None,
        cli_hint: str | None = None,
        default_branch: str | None = None,
        created_by: str | None = None,
        is_global: bool = False,
    ) -> Dict[str, Any]:
        """Create a new datasource.

        Args:
            name: User-provided label
            ds_type: Datasource type ('generic', 'repository', 'postgresql',
                     'neo4j', 'mongodb', 'webdav')
            connection_url: Connection string (nullable for generic)
            description: What this datasource contains
            credentials: Auth details (env_vars dict for generic,
                         auth_method+token/ssh_key for repository,
                         type-specific for managed connectors)
            job_id: Job UUID (None for global)
            cli_hint: Suggested CLI command
            default_branch: Branch to clone (repository type)
            created_by: Owner user UUID
            is_global: Whether this datasource is visible to all users

        Returns:
            Created datasource dict

        Raises:
            asyncpg.UniqueViolationError: If a datasource with the same
                name+type already exists for the same owner
        """
        job_uuid = UUID(job_id) if job_id else None
        owner_uuid = UUID(created_by) if created_by else None

        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO datasources (name, description, type, connection_url,
                                         credentials, job_id, cli_hint, default_branch,
                                         created_by, is_global)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                RETURNING id, name, description, type, connection_url, credentials,
                          job_id, cli_hint, default_branch, created_by, is_global,
                          created_at, updated_at
                """,
                name,
                description,
                ds_type,
                connection_url,
                json.dumps(credentials) if credentials else "{}",
                job_uuid,
                cli_hint,
                default_branch,
                owner_uuid,
                is_global,
            )

        return dict(row)

    async def update_datasource(
        self,
        datasource_id: str,
        name: str | None = None,
        description: str | None = None,
        connection_url: str | None = None,
        credentials: Dict[str, Any] | None = None,
        cli_hint: str | None = None,
        default_branch: str | None = None,
    ) -> bool:
        """Update a datasource.

        Args:
            datasource_id: Datasource UUID
            name: New name
            description: New description
            connection_url: New connection URL
            credentials: New credentials
            cli_hint: New CLI hint
            default_branch: New default branch

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

        if cli_hint is not None:
            param_count += 1
            updates.append(f"cli_hint = ${param_count}")
            values.append(cli_hint)

        if default_branch is not None:
            param_count += 1
            updates.append(f"default_branch = ${param_count}")
            values.append(default_branch)

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
        """Resolve datasources for a job.

        Returns all applicable datasources: those linked to the job's
        project via the project_datasources junction table, plus any
        unlinked global datasources. Multiple datasources of the same
        type are allowed.

        For project-linked datasources, includes the project-level
        read_only setting which controls the access mode (CLI vs tools).

        Args:
            job_id: Job UUID
            project_id: Optional project UUID for project-level datasources

        Returns:
            List of resolved datasource dicts (may contain multiple per type)
        """
        try:
            UUID(job_id)
        except ValueError:
            return []

        project_uuid = UUID(project_id) if project_id else None

        async with self.acquire() as conn:
            if project_uuid:
                rows = await conn.fetch(
                    """
                    SELECT DISTINCT d.id, d.name, d.description, d.type,
                        d.connection_url, d.credentials,
                        d.cli_hint, d.default_branch,
                        d.created_at, d.updated_at,
                        pd.read_only AS project_read_only
                    FROM datasources d
                    LEFT JOIN project_datasources pd
                        ON pd.datasource_id = d.id AND pd.project_id = $1
                    WHERE pd.project_id IS NOT NULL
                       OR NOT EXISTS (
                           SELECT 1 FROM project_datasources pd2
                           WHERE pd2.datasource_id = d.id
                       )
                    ORDER BY d.type, d.name
                    """,
                    project_uuid,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT DISTINCT d.id, d.name, d.description, d.type,
                        d.connection_url, d.credentials,
                        d.cli_hint, d.default_branch,
                        d.created_at, d.updated_at,
                        NULL::boolean AS project_read_only
                    FROM datasources d
                    WHERE NOT EXISTS (
                        SELECT 1 FROM project_datasources pd2
                        WHERE pd2.datasource_id = d.id
                    )
                    ORDER BY d.type, d.name
                    """,
                )

        return [dict(row) for row in rows]

    async def resolve_datasources_for_thread(
        self,
        datasource_ids: list[str] | None = None,
        project_ids: list[str] | None = None,
    ) -> List[Dict[str, Any]]:
        """Resolve datasources for a persistent thread.

        Returns all applicable datasources: explicitly attached by ID,
        plus those linked to the thread's projects via project_datasources,
        plus unlinked global datasources. Multiple datasources of the
        same type are allowed.

        Args:
            datasource_ids: Explicit datasource UUIDs attached to the thread
            project_ids: Project UUIDs scoped to the thread

        Returns:
            List of resolved datasource dicts (may contain multiple per type)
        """
        ds_uuids = []
        for ds_id in datasource_ids or []:
            try:
                ds_uuids.append(UUID(ds_id))
            except ValueError:
                pass

        proj_uuids = []
        for pid in project_ids or []:
            try:
                proj_uuids.append(UUID(pid))
            except ValueError:
                pass

        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT d.id, d.name, d.description, d.type,
                    d.connection_url, d.credentials,
                    d.cli_hint, d.default_branch,
                    d.created_at, d.updated_at,
                    pd.read_only AS project_read_only
                FROM datasources d
                LEFT JOIN project_datasources pd
                    ON pd.datasource_id = d.id
                   AND pd.project_id = ANY($2::uuid[])
                WHERE d.id = ANY($1::uuid[])
                   OR pd.project_id IS NOT NULL
                   OR (
                       d.is_global = true
                       AND NOT EXISTS (
                           SELECT 1 FROM project_datasources pd2
                           WHERE pd2.datasource_id = d.id
                       )
                   )
                ORDER BY d.type, d.name
                """,
                ds_uuids,
                proj_uuids,
            )

        return [dict(row) for row in rows]

    # -- Project ↔ Datasource junction (N:M) ----------------------------------

    async def link_datasource_to_project(
        self,
        project_id: str,
        datasource_id: str,
        read_only: bool | None = None,
        description: str | None = None,
    ) -> bool:
        """Link a datasource to a project with optional overrides.

        Returns True if newly linked, False if already linked.
        """
        try:
            p_uuid = UUID(project_id)
            d_uuid = UUID(datasource_id)
        except ValueError:
            return False

        async with self.acquire() as conn:
            result = await conn.execute(
                """
                INSERT INTO project_datasources (project_id, datasource_id, read_only, description)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (project_id, datasource_id) DO UPDATE SET
                    read_only = EXCLUDED.read_only,
                    description = EXCLUDED.description
                """,
                p_uuid,
                d_uuid,
                read_only,
                description,
            )

        return "INSERT" in result or "UPDATE" in result

    async def unlink_datasource_from_project(
        self, project_id: str, datasource_id: str
    ) -> bool:
        """Unlink a datasource from a project.

        Returns True if unlinked, False if not found.
        """
        try:
            p_uuid = UUID(project_id)
            d_uuid = UUID(datasource_id)
        except ValueError:
            return False

        async with self.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM project_datasources WHERE project_id = $1 AND datasource_id = $2",
                p_uuid,
                d_uuid,
            )

        return result == "DELETE 1"

    async def list_project_datasources(self, project_id: str) -> List[Dict[str, Any]]:
        """List all datasources linked to a project.

        Returns datasource details with project-level settings (read_only,
        description override).
        """
        try:
            p_uuid = UUID(project_id)
        except ValueError:
            return []

        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT d.id, d.name,
                       COALESCE(pd.description, d.description) AS description,
                       d.type, d.connection_url, d.credentials,
                       d.cli_hint, d.default_branch,
                       d.job_id, d.created_at, d.updated_at,
                       pd.linked_at,
                       pd.read_only AS project_read_only,
                       pd.description AS project_description
                FROM datasources d
                JOIN project_datasources pd ON pd.datasource_id = d.id
                WHERE pd.project_id = $1
                ORDER BY pd.linked_at DESC
                """,
                p_uuid,
            )

        return [dict(row) for row in rows]

    async def update_project_datasource(
        self,
        project_id: str,
        datasource_id: str,
        read_only: bool | None = ...,
        description: str | None = ...,
    ) -> bool:
        """Update project-level overrides for a linked datasource.

        Pass None to clear an override (fall back to datasource default).
        Uses sentinel default (...) so None is a valid value to set.
        """
        try:
            p_uuid = UUID(project_id)
            d_uuid = UUID(datasource_id)
        except ValueError:
            return False

        set_parts = []
        values: list = [p_uuid, d_uuid]
        idx = 3

        if read_only is not ...:
            set_parts.append(f"read_only = ${idx}")
            values.append(read_only)
            idx += 1

        if description is not ...:
            set_parts.append(f"description = ${idx}")
            values.append(description)
            idx += 1

        if not set_parts:
            return True

        async with self.acquire() as conn:
            result = await conn.execute(
                f"""
                UPDATE project_datasources SET {", ".join(set_parts)}
                WHERE project_id = $1 AND datasource_id = $2
                """,
                *values,
            )

        return result == "UPDATE 1"

    async def list_datasource_projects(self, datasource_id: str) -> List[str]:
        """Return project IDs linked to a datasource."""
        try:
            d_uuid = UUID(datasource_id)
        except ValueError:
            return []

        async with self.acquire() as conn:
            rows = await conn.fetch(
                "SELECT project_id FROM project_datasources WHERE datasource_id = $1",
                d_uuid,
            )

        return [str(row["project_id"]) for row in rows]

    async def upsert_default_datasource(
        self,
        name: str,
        ds_type: str,
        connection_url: str,
        credentials: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Create or update a system-seeded datasource (created_by=NULL).

        Used during init to seed default datasources from env vars.
        These are always global (is_global=TRUE) and have no owner.

        Args:
            name: Datasource label
            ds_type: Datasource type
            connection_url: Connection URL
            credentials: Additional auth details

        Returns:
            Created or updated datasource dict
        """
        creds_json = json.dumps(credentials) if credentials else "{}"

        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO datasources (name, type, connection_url, credentials,
                                         created_by, is_global)
                VALUES ($1, $2, $3, $4, NULL, TRUE)
                ON CONFLICT (name, type, COALESCE(created_by, '00000000-0000-0000-0000-000000000000'))
                DO UPDATE SET
                    connection_url = EXCLUDED.connection_url,
                    credentials = EXCLUDED.credentials,
                    is_global = TRUE
                RETURNING id, name, description, type, connection_url, credentials,
                          created_by, is_global, created_at, updated_at
                """,
                name,
                ds_type,
                connection_url,
                creds_json,
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
                              main_cloud_backend, main_cloud_folder_handle,
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
    # AUTH TOKEN OPERATIONS  (consolidated mcp_tokens + PATs — see
    # docs/features/auth_bff_and_api_tokens.md §3)
    #
    # Two kinds live in one table:
    #   - kind='mcp' — legacy Claude-Code/MCP tokens (srw_<32-byte>); scope
    #     column carries 'user' / 'all' / 'project:<uuid>'. Kept untouched
    #     so the MCP server's existing TokenVerifier flow keeps working.
    #   - kind='api' — Personal Access Tokens (ak_<43-char>); scopes column
    #     carries action-level scope strings ('jobs:read', 'chat:write', …).
    #
    # The two halves stay separate at the helper level so MCP-server calls
    # and PAT calls never cross-pollinate. The Bearer validator dispatches
    # by token prefix and then calls the kind-specific helper.
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
        last_four: str | None = None,
    ) -> Dict[str, Any]:
        """Create a new MCP API token (kind='mcp')."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO auth_tokens (user_id, name, token_hash, token_prefix,
                                         kind, scope, expires_at, origin, last_four)
                VALUES ($1, $2, $3, $4, 'mcp', $5, $6, $7, $8)
                RETURNING id, user_id, name, token_prefix, scope, origin,
                          expires_at, revoked_at, last_used_at, created_at, last_four
                """,
                user_id,
                name,
                token_hash,
                token_prefix,
                scope,
                expires_at,
                origin,
                last_four,
            )
            return dict(row)

    async def get_mcp_token_by_hash(self, token_hash: str) -> Dict[str, Any] | None:
        """Look up an active MCP token by its hash. Returns None if revoked/expired/wrong kind."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT t.id, t.user_id, t.name, t.token_prefix, t.scope,
                       t.expires_at, t.last_used_at, t.created_at,
                       u.display_name, u.email
                FROM auth_tokens t
                JOIN users u ON u.id = t.user_id
                WHERE t.token_hash = $1
                  AND t.kind = 'mcp'
                  AND t.revoked_at IS NULL
                  AND (t.expires_at IS NULL OR t.expires_at > CURRENT_TIMESTAMP)
                """,
                token_hash,
            )
            return dict(row) if row else None

    async def list_mcp_tokens(self, user_id: str) -> List[Dict[str, Any]]:
        """List all MCP tokens for a user (excludes token_hash). kind='mcp' only."""
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, user_id, name, token_prefix, scope, last_four,
                       expires_at, revoked_at, last_used_at, created_at
                FROM auth_tokens
                WHERE user_id = $1 AND kind = 'mcp'
                ORDER BY created_at DESC
                """,
                user_id,
            )
            return [dict(r) for r in rows]

    async def revoke_mcp_token(self, token_id: str, user_id: str) -> bool:
        """Revoke an MCP token. Returns True if a token was revoked.

        Guards on kind='mcp' so a cockpit user can't accidentally revoke a
        PAT via the MCP token UI (and vice versa).
        """
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE auth_tokens SET revoked_at = CURRENT_TIMESTAMP
                WHERE id = $1 AND user_id = $2 AND kind = 'mcp'
                  AND revoked_at IS NULL
                """,
                token_id,
                user_id,
            )
            return result == "UPDATE 1"

    async def update_mcp_token_last_used(self, token_hash: str) -> None:
        """Update the last_used_at timestamp for an MCP token.

        Used by the MCP server's /api/internal/mcp-token-verify path,
        which only has the hash at hand (no IP).
        """
        async with self.acquire() as conn:
            await conn.execute(
                "UPDATE auth_tokens SET last_used_at = CURRENT_TIMESTAMP "
                "WHERE token_hash = $1 AND kind = 'mcp'",
                token_hash,
            )

    async def cleanup_expired_mcp_tokens(self) -> None:
        """Delete expired and long-revoked auth tokens (both kinds).

        Same cadence and lifecycle for both — kind='mcp' rows hit this
        path via the legacy MCP server flow; kind='api' rows hit it via
        the same cleanup loop in the orchestrator.
        """
        async with self.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM auth_tokens
                WHERE (expires_at IS NOT NULL AND expires_at < CURRENT_TIMESTAMP)
                   OR (revoked_at IS NOT NULL AND revoked_at < CURRENT_TIMESTAMP - INTERVAL '30 days')
                """
            )
            # Rotation grace: revoke kind='api' rows whose successor is
            # older than 24h. The kind-mcp path doesn't rotate (no UI
            # surface), so the gate is fine kind-agnostic — superseded_by
            # is only ever set by the PAT rotate flow.
            await conn.execute(
                """
                UPDATE auth_tokens
                   SET revoked_at = CURRENT_TIMESTAMP
                 WHERE revoked_at IS NULL
                   AND superseded_by IS NOT NULL
                   AND id IN (
                       SELECT a.id FROM auth_tokens a
                       JOIN auth_tokens b ON a.superseded_by = b.id
                       WHERE b.created_at < CURRENT_TIMESTAMP - INTERVAL '24 hours'
                   )
                """
            )

    # ── kind='api' (PAT) helpers ────────────────────────────────────────────

    async def create_api_key(
        self,
        *,
        user_id: str,
        name: str,
        token_hash: str,
        token_prefix: str,
        last_four: str,
        scopes: List[str],
        expires_at=None,
    ) -> Dict[str, Any]:
        """Create a new PAT (kind='api')."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO auth_tokens (user_id, name, token_hash, token_prefix,
                                         kind, scopes, last_four, expires_at)
                VALUES ($1, $2, $3, $4, 'api', $5, $6, $7)
                RETURNING id, user_id, name, token_prefix, last_four, scopes,
                          expires_at, revoked_at, last_used_at, created_at,
                          superseded_by
                """,
                user_id,
                name,
                token_hash,
                token_prefix,
                scopes,
                last_four,
                expires_at,
            )
            return dict(row)

    async def list_api_keys(self, user_id: str) -> List[Dict[str, Any]]:
        """List all PATs for a user. Excludes token_hash."""
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, user_id, name, token_prefix, last_four, scopes,
                       expires_at, revoked_at, last_used_at, last_used_ip,
                       created_at, superseded_by
                FROM auth_tokens
                WHERE user_id = $1 AND kind = 'api'
                ORDER BY created_at DESC
                """,
                user_id,
            )
            return [dict(r) for r in rows]

    async def revoke_api_key(self, token_id: str, user_id: str) -> bool:
        """Revoke a PAT (kind='api'). Returns True on success."""
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE auth_tokens SET revoked_at = CURRENT_TIMESTAMP
                WHERE id = $1 AND user_id = $2 AND kind = 'api'
                  AND revoked_at IS NULL
                """,
                token_id,
                user_id,
            )
            return result == "UPDATE 1"

    async def rotate_api_key(
        self,
        *,
        old_id: str,
        user_id: str,
        token_hash: str,
        token_prefix: str,
        last_four: str,
    ) -> Dict[str, Any] | None:
        """Issue a successor for a PAT, name+scopes+expiry inherited.

        Sets ``old.superseded_by = new.id``. The old row stays valid for
        24h (cleanup loop revokes it). Returns the new row, or None if
        the old token wasn't found / belonged to another user / wrong kind.
        """
        async with self.acquire() as conn:
            async with conn.transaction():
                old = await conn.fetchrow(
                    """
                    SELECT id, name, scopes, expires_at
                    FROM auth_tokens
                    WHERE id = $1 AND user_id = $2 AND kind = 'api'
                      AND revoked_at IS NULL
                    """,
                    old_id,
                    user_id,
                )
                if not old:
                    return None
                new = await conn.fetchrow(
                    """
                    INSERT INTO auth_tokens (user_id, name, token_hash,
                                             token_prefix, kind, scopes,
                                             last_four, expires_at)
                    VALUES ($1, $2, $3, $4, 'api', $5, $6, $7)
                    RETURNING id, user_id, name, token_prefix, last_four,
                              scopes, expires_at, revoked_at, last_used_at,
                              created_at, superseded_by
                    """,
                    user_id,
                    old["name"],
                    token_hash,
                    token_prefix,
                    list(old["scopes"] or []),
                    last_four,
                    old["expires_at"],
                )
                await conn.execute(
                    "UPDATE auth_tokens SET superseded_by = $1 WHERE id = $2",
                    new["id"],
                    old["id"],
                )
                return dict(new)

    # ── Cross-kind helpers used by the Bearer validator ─────────────────────

    async def get_auth_token_by_hash(self, token_hash: str) -> Dict[str, Any] | None:
        """Look up an active auth token by hash (either kind). The validator
        prefix-sniffs the token format and then enforces the kind matches
        before trusting the row — this method just returns whatever's in
        the table that hasn't expired or been revoked.
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT t.id, t.user_id, t.name, t.kind, t.token_prefix,
                       t.scope, t.scopes, t.expires_at, t.last_used_at,
                       t.created_at, t.origin,
                       u.display_name, u.email
                FROM auth_tokens t
                JOIN users u ON u.id = t.user_id
                WHERE t.token_hash = $1
                  AND t.revoked_at IS NULL
                  AND (t.expires_at IS NULL OR t.expires_at > CURRENT_TIMESTAMP)
                """,
                token_hash,
            )
            return dict(row) if row else None

    async def touch_auth_token(self, token_id: str, ip: str | None) -> None:
        """Fire-and-forget: bump last_used_at + last_used_ip after a Bearer
        validation. Schema cast on the IP is best-effort; an unparseable
        client.host (e.g. behind a misbehaving proxy) gets stored as NULL.
        """
        async with self.acquire() as conn:
            await conn.execute(
                """
                UPDATE auth_tokens
                   SET last_used_at = CURRENT_TIMESTAMP,
                       last_used_ip = $2::inet
                 WHERE id = $1
                """,
                token_id,
                ip,
            )

    # =========================================================================
    # BFF SESSION OPERATIONS  (Cookie BFF — see auth_bff_and_api_tokens.md §1.2)
    # =========================================================================

    async def create_srw_session(
        self,
        *,
        user_id: str,
        kc_sub: str,
        access_token: str,
        refresh_token: str,
        id_token: str,
        access_expires_at,
        absolute_expires_at,
        kc_sid: str | None = None,
        user_agent: str | None = None,
        created_ip: str | None = None,
    ) -> str:
        """Insert a new BFF session row and return its UUID (the cookie value)."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO srw_sessions (
                    user_id, kc_sub, kc_sid,
                    access_token, refresh_token, id_token,
                    access_expires_at, absolute_expires_at,
                    user_agent, created_ip
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                RETURNING id
                """,
                user_id,
                kc_sub,
                kc_sid,
                access_token,
                refresh_token,
                id_token,
                access_expires_at,
                absolute_expires_at,
                user_agent,
                created_ip,
            )
            return str(row["id"])

    async def get_srw_session(self, session_id: str) -> Dict[str, Any] | None:
        """Fetch an un-revoked session row by UUID. Returns None for revoked rows.

        Idle/absolute-expiry are enforced by the validator (which holds the
        clock), not here — the validator needs the row in either case to
        decide whether to refresh or kill the session.
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, user_id, kc_sub, kc_sid,
                       access_token, refresh_token, id_token,
                       access_expires_at, absolute_expires_at,
                       created_at, last_seen_at, user_agent, created_ip
                FROM srw_sessions
                WHERE id = $1 AND revoked_at IS NULL
                """,
                session_id,
            )
            return dict(row) if row else None

    async def touch_srw_session_last_seen(self, session_id: str) -> None:
        """Bump last_seen_at to now() — anchors the idle-timeout window."""
        async with self.acquire() as conn:
            await conn.execute(
                "UPDATE srw_sessions SET last_seen_at = now() WHERE id = $1",
                session_id,
            )

    async def refresh_srw_session_tokens(
        self,
        session_id: str,
        *,
        access_token: str,
        refresh_token: str,
        access_expires_at,
        id_token: str | None = None,
    ) -> None:
        """Write back fresh tokens after a successful KC refresh.

        Keycloak may or may not rotate the refresh token; we always store
        whatever it gave us. id_token is preserved if KC didn't issue a new
        one (some flows omit it on refresh — we keep the original so logout
        still has a valid id_token_hint).
        """
        async with self.acquire() as conn:
            if id_token is None:
                await conn.execute(
                    """
                    UPDATE srw_sessions
                       SET access_token = $2,
                           refresh_token = $3,
                           access_expires_at = $4,
                           last_seen_at = now()
                     WHERE id = $1
                    """,
                    session_id,
                    access_token,
                    refresh_token,
                    access_expires_at,
                )
            else:
                await conn.execute(
                    """
                    UPDATE srw_sessions
                       SET access_token = $2,
                           refresh_token = $3,
                           id_token = $4,
                           access_expires_at = $5,
                           last_seen_at = now()
                     WHERE id = $1
                    """,
                    session_id,
                    access_token,
                    refresh_token,
                    id_token,
                    access_expires_at,
                )

    async def delete_srw_session(self, session_id: str) -> None:
        """Hard-delete a session row. Used by /auth/logout and the session-
        fixation defense in /auth/callback (kill the old cookie's row before
        issuing a new one).
        """
        async with self.acquire() as conn:
            await conn.execute(
                "DELETE FROM srw_sessions WHERE id = $1",
                session_id,
            )

    async def delete_srw_sessions_by_kc_sid(self, kc_sid: str) -> int:
        """Delete every session row tied to a Keycloak SID. Used by the
        /auth/backchannel-logout endpoint when KC tells us the user logged
        out at the IdP. Returns the number of rows deleted (for audit).
        """
        async with self.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM srw_sessions WHERE kc_sid = $1",
                kc_sid,
            )
            # asyncpg returns 'DELETE N'
            try:
                return int(result.split()[-1])
            except (ValueError, IndexError):
                return 0

    async def cleanup_expired_srw_sessions(self) -> None:
        """Delete sessions past their absolute lifetime, plus any revoked
        sessions older than 7 days (keeps audit visibility for a week).
        """
        async with self.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM srw_sessions
                WHERE absolute_expires_at < now()
                   OR (revoked_at IS NOT NULL AND revoked_at < now() - INTERVAL '7 days')
                """
            )

    async def create_srw_pre_auth(
        self,
        *,
        state: str,
        pkce_verifier: str,
        return_to: str,
        ttl_seconds: int = 300,
    ) -> str:
        """Park OAuth state + PKCE verifier for the /auth/callback round-trip.

        Returns the row UUID (which becomes the srw_pre_auth cookie value).
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO srw_pre_auth_states (state, pkce_verifier, return_to, expires_at)
                VALUES ($1, $2, $3, now() + ($4 || ' seconds')::interval)
                RETURNING id
                """,
                state,
                pkce_verifier,
                return_to,
                str(ttl_seconds),
            )
            return str(row["id"])

    async def consume_srw_pre_auth(self, pre_auth_id: str) -> Dict[str, Any] | None:
        """Single-use consumption of a pre-auth row. CAS on consumed_at.

        Returns the row contents iff it was un-consumed and unexpired at
        consumption time; None otherwise. Mirrors the magic_link_tokens
        pattern (0006_headless_notifications.sql).
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE srw_pre_auth_states
                   SET consumed_at = now()
                 WHERE id = $1
                   AND consumed_at IS NULL
                   AND expires_at > now()
                RETURNING state, pkce_verifier, return_to
                """,
                pre_auth_id,
            )
            return dict(row) if row else None

    async def cleanup_expired_srw_pre_auth(self) -> None:
        """Delete pre-auth rows past TTL or consumed > 1 hour ago."""
        async with self.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM srw_pre_auth_states
                WHERE expires_at < now()
                   OR (consumed_at IS NOT NULL AND consumed_at < now() - INTERVAL '1 hour')
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
                encrypt(api_key),
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
            stored = await conn.fetchval(
                "SELECT api_key FROM user_api_keys WHERE user_id = $1 AND provider = $2",
                UUID(user_id),
                provider,
            )
        return _decrypt_stored(stored, field=f"user_api_keys[{provider}]")

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
                encrypt(api_key),
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
            stored = await conn.fetchval(
                "SELECT api_key FROM project_api_keys WHERE project_id = $1 AND provider = $2",
                UUID(project_id),
                provider,
            )
        return _decrypt_stored(stored, field=f"project_api_keys[{provider}]")

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
        """Resolve all API keys for a job.

        Precedence (lowest → highest): system → project → user. Higher-
        precedence values overwrite lower ones in the returned dict.
        Returns a dict mapping provider -> api_key for every provider
        where at least one key is configured.
        """
        resolved: Dict[str, str] = {}

        # System-level keys (lowest precedence; replaces env-var fallback)
        async with self.acquire() as conn:
            rows = await conn.fetch("SELECT provider, api_key FROM system_api_keys")
            for row in rows:
                plain = _decrypt_stored(
                    row["api_key"], field=f"system_api_keys[{row['provider']}]"
                )
                if plain is not None:
                    resolved[row["provider"]] = plain

        # Project keys override system
        if project_id:
            async with self.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT provider, api_key FROM project_api_keys WHERE project_id = $1",
                    UUID(project_id),
                )
                for row in rows:
                    plain = _decrypt_stored(
                        row["api_key"], field=f"project_api_keys[{row['provider']}]"
                    )
                    if plain is not None:
                        resolved[row["provider"]] = plain

        # User keys override project and system
        if user_id:
            async with self.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT provider, api_key FROM user_api_keys WHERE user_id = $1",
                    UUID(user_id),
                )
                for row in rows:
                    plain = _decrypt_stored(
                        row["api_key"], field=f"user_api_keys[{row['provider']}]"
                    )
                    if plain is not None:
                        resolved[row["provider"]] = plain

        return resolved

    # =========================================================================
    # USER LLM ENDPOINT OPERATIONS
    # =========================================================================

    async def list_user_llm_endpoints(self, user_id: str) -> List[Dict[str, Any]]:
        """List a user's LLM endpoints with their model rows.

        Returns one dict per endpoint with a nested ``models`` list.
        API keys are never returned — only ``key_prefix`` for display.
        """
        async with self.acquire() as conn:
            endpoint_rows = await conn.fetch(
                """
                SELECT id, label, base_url, key_prefix, created_at, updated_at
                FROM llm_endpoints
                WHERE user_id = $1
                ORDER BY label
                """,
                UUID(user_id),
            )
            if not endpoint_rows:
                return []

        # Endpoint-model rows used to live on user_llm_endpoint_models; that
        # table was retired when the admin-curated catalog became the
        # single source of truth. The "models" key is kept on the response
        # for shape compatibility with the Cockpit endpoints page.
        return [dict(e, models=[]) for e in endpoint_rows]

    async def get_user_llm_endpoint(
        self, endpoint_id: str, user_id: str | None = None
    ) -> Dict[str, Any] | None:
        """Fetch a single endpoint row (including api_key — for dispatch only).

        If ``user_id`` is provided, the query is scoped to that user for
        authorization; callers that have already checked ownership can omit it.
        """
        query = """
            SELECT id, user_id, label, base_url, api_key, key_prefix,
                   created_at, updated_at
            FROM llm_endpoints
            WHERE id = $1
        """
        args: List[Any] = [UUID(endpoint_id)]
        if user_id is not None:
            query += " AND user_id = $2"
            args.append(UUID(user_id))

        async with self.acquire() as conn:
            row = await conn.fetchrow(query, *args)
        if row is None:
            return None
        result = dict(row)
        result["api_key"] = _decrypt_stored(
            result.get("api_key"), field=f"llm_endpoints[{result['id']}].api_key"
        )
        return result

    async def create_user_llm_endpoint(
        self,
        user_id: str,
        label: str,
        base_url: str,
        api_key: str | None,
        key_prefix: str | None,
    ) -> Dict[str, Any]:
        """Create a new LLM endpoint for a user. Label must be unique per user."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO llm_endpoints
                    (user_id, label, base_url, api_key, key_prefix)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, label, base_url, key_prefix, created_at, updated_at
                """,
                UUID(user_id),
                label,
                base_url,
                _encrypt_optional(api_key),
                key_prefix,
            )
            return dict(row)

    async def update_user_llm_endpoint(
        self,
        endpoint_id: str,
        user_id: str,
        label: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        key_prefix: str | None = None,
        clear_api_key: bool = False,
    ) -> Dict[str, Any] | None:
        """Patch an endpoint. Only non-None fields are updated.

        ``clear_api_key=True`` explicitly nulls the key (for endpoints that
        transition from authenticated to anonymous).
        """
        sets: List[str] = []
        args: List[Any] = [UUID(endpoint_id), UUID(user_id)]
        param_idx = 3
        if label is not None:
            sets.append(f"label = ${param_idx}")
            args.append(label)
            param_idx += 1
        if base_url is not None:
            sets.append(f"base_url = ${param_idx}")
            args.append(base_url)
            param_idx += 1
        if clear_api_key:
            sets.append("api_key = NULL")
            sets.append("key_prefix = NULL")
        elif api_key is not None:
            sets.append(f"api_key = ${param_idx}")
            args.append(encrypt(api_key))
            param_idx += 1
            if key_prefix is not None:
                sets.append(f"key_prefix = ${param_idx}")
                args.append(key_prefix)
                param_idx += 1

        if not sets:
            # Nothing to change — return current row.
            return await self.get_user_llm_endpoint(endpoint_id, user_id)

        sets.append("updated_at = CURRENT_TIMESTAMP")
        query = f"""
            UPDATE llm_endpoints
            SET {", ".join(sets)}
            WHERE id = $1 AND user_id = $2
            RETURNING id, label, base_url, key_prefix, created_at, updated_at
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(query, *args)
            return dict(row) if row else None

    async def delete_user_llm_endpoint(self, endpoint_id: str, user_id: str) -> bool:
        """Delete an endpoint and cascade to its model rows."""
        async with self.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM llm_endpoints WHERE id = $1 AND user_id = $2",
                UUID(endpoint_id),
                UUID(user_id),
            )
            return result == "DELETE 1"

    # The user-side endpoint-model accessors (create/update/delete/resolve)
    # were removed when the admin-curated `models` catalog became the single
    # source of truth. User-scoped endpoints survive as transports only;
    # offerings are resolved via PostgresDB.resolve_catalog_model.

    # =========================================================================
    # SYSTEM API KEY OPERATIONS
    # Provider-level keys shared across users, seeded by helm or managed via
    # Admin → Providers. Consulted by resolve_api_keys_for_job after user/
    # project keys; replaces the legacy env-var fallback.
    # =========================================================================

    async def list_system_api_keys(self) -> List[Dict[str, Any]]:
        """List system API keys (prefix only, no full key)."""
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, provider, key_prefix, label, seeded_from,
                       created_at, updated_at
                FROM system_api_keys
                ORDER BY provider
                """
            )
            return [dict(r) for r in rows]

    async def get_system_api_key(self, provider: str) -> str | None:
        """Get the full (decrypted) API key for a provider, or None."""
        async with self.acquire() as conn:
            stored = await conn.fetchval(
                "SELECT api_key FROM system_api_keys WHERE provider = $1",
                provider,
            )
        return _decrypt_stored(stored, field=f"system_api_keys[{provider}]")

    async def upsert_system_api_key(
        self,
        provider: str,
        api_key: str,
        key_prefix: str,
        label: str | None = None,
        seeded_from: str | None = None,
    ) -> Dict[str, Any]:
        """Create or replace the system-level API key for a provider.

        ``seeded_from`` is a breadcrumb set by the helm seed job; admin-UI
        edits pass ``None`` so subsequent re-seeds skip overwriting.
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO system_api_keys
                    (provider, api_key, key_prefix, label, seeded_from)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (provider) DO UPDATE
                SET api_key = EXCLUDED.api_key,
                    key_prefix = EXCLUDED.key_prefix,
                    label = EXCLUDED.label,
                    seeded_from = EXCLUDED.seeded_from,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING id, provider, key_prefix, label, seeded_from,
                          created_at, updated_at
                """,
                provider,
                encrypt(api_key),
                key_prefix,
                label,
                seeded_from,
            )
            return dict(row)

    async def delete_system_api_key(self, provider: str) -> bool:
        """Remove the system-level key for a provider."""
        async with self.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM system_api_keys WHERE provider = $1",
                provider,
            )
            return result == "DELETE 1"

    async def set_system_api_key_discovery_cache(
        self,
        provider: str,
        payload: Dict[str, Any] | None,
    ) -> bool:
        """Stage (or clear) the discovery payload for a provider key.

        ``payload=None`` clears the cache (e.g. on key rotation, before the
        async re-discovery completes). Returns True iff a row was updated.
        """
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE system_api_keys
                   SET discovery_cache_json = $2,
                       discovery_cache_at = CASE
                           WHEN $2::JSONB IS NULL THEN NULL
                           ELSE CURRENT_TIMESTAMP
                       END
                 WHERE provider = $1
                """,
                provider,
                json.dumps(payload) if payload is not None else None,
            )
            return result == "UPDATE 1"

    async def get_system_api_key_discovery_cache(
        self, provider: str
    ) -> Dict[str, Any] | None:
        """Return the cached discovery payload + timestamp, or None.

        The returned dict carries ``payload`` (the cockpit-ready candidate
        list, exactly as ``build_cache_payload`` shaped it) and ``cached_at``
        as an ISO-8601 string for staleness math on the client side.
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT discovery_cache_json, discovery_cache_at
                  FROM system_api_keys
                 WHERE provider = $1
                """,
                provider,
            )
        if row is None or row["discovery_cache_json"] is None:
            return None
        cached_at = row["discovery_cache_at"]
        cache_json = row["discovery_cache_json"]
        if isinstance(cache_json, str):
            cache_json = json.loads(cache_json)
        return {
            "payload": cache_json,
            "cached_at": cached_at.isoformat() if cached_at else None,
        }

    # =========================================================================
    # SYSTEM LLM ENDPOINT OPERATIONS
    # System-scoped endpoints (user_id IS NULL) are visible to every user.
    # They are seeded by helm on fresh install and edited via Admin → Providers.
    # Same llm_endpoints table as the user-scoped ops above; only the scoping
    # filter (user_id IS NULL vs user_id = $1) differs.
    # =========================================================================

    async def list_system_llm_endpoints(self) -> List[Dict[str, Any]]:
        """List system-scoped endpoints (transport rows only).

        API keys are never returned — only ``key_prefix`` for display. The
        ``models`` key is kept on the response shape for Cockpit
        compatibility but is always empty: model offerings live in the
        admin-curated catalog (``models`` table) and are queried
        independently via list_models.
        """
        async with self.acquire() as conn:
            endpoint_rows = await conn.fetch(
                """
                SELECT id, label, base_url, key_prefix, created_at, updated_at
                FROM llm_endpoints
                WHERE user_id IS NULL
                ORDER BY label
                """
            )
        return [dict(e, models=[]) for e in endpoint_rows]

    async def get_system_llm_endpoint(self, endpoint_id: str) -> Dict[str, Any] | None:
        """Fetch a single system endpoint row (with decrypted api_key).

        Returns None when the endpoint does not exist or is user-scoped.
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, label, base_url, api_key, key_prefix,
                       created_at, updated_at
                FROM llm_endpoints
                WHERE id = $1 AND user_id IS NULL
                """,
                UUID(endpoint_id),
            )
        if row is None:
            return None
        result = dict(row)
        result["api_key"] = _decrypt_stored(
            result.get("api_key"),
            field=f"llm_endpoints[{result['id']}].api_key",
        )
        return result

    async def create_system_llm_endpoint(
        self,
        label: str,
        base_url: str,
        api_key: str | None,
        key_prefix: str | None,
    ) -> Dict[str, Any]:
        """Create a new system-scoped LLM endpoint. Label must be globally unique."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO llm_endpoints
                    (user_id, label, base_url, api_key, key_prefix)
                VALUES (NULL, $1, $2, $3, $4)
                RETURNING id, label, base_url, key_prefix, created_at, updated_at
                """,
                label,
                base_url,
                _encrypt_optional(api_key),
                key_prefix,
            )
            return dict(row)

    async def update_system_llm_endpoint(
        self,
        endpoint_id: str,
        label: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        key_prefix: str | None = None,
        clear_api_key: bool = False,
    ) -> Dict[str, Any] | None:
        """Patch a system endpoint. Only non-None fields are updated.

        Returns None if no row matches (endpoint missing or user-scoped).
        """
        sets: List[str] = []
        args: List[Any] = [UUID(endpoint_id)]
        param_idx = 2
        if label is not None:
            sets.append(f"label = ${param_idx}")
            args.append(label)
            param_idx += 1
        if base_url is not None:
            sets.append(f"base_url = ${param_idx}")
            args.append(base_url)
            param_idx += 1
        if clear_api_key:
            sets.append("api_key = NULL")
            sets.append("key_prefix = NULL")
        elif api_key is not None:
            sets.append(f"api_key = ${param_idx}")
            args.append(encrypt(api_key))
            param_idx += 1
            if key_prefix is not None:
                sets.append(f"key_prefix = ${param_idx}")
                args.append(key_prefix)
                param_idx += 1

        if not sets:
            # No-op patch — just return current row for parity with user variant.
            async with self.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT id, label, base_url, key_prefix, created_at, updated_at
                    FROM llm_endpoints
                    WHERE id = $1 AND user_id IS NULL
                    """,
                    UUID(endpoint_id),
                )
                return dict(row) if row else None

        sets.append("updated_at = CURRENT_TIMESTAMP")
        query = f"""
            UPDATE llm_endpoints
            SET {", ".join(sets)}
            WHERE id = $1 AND user_id IS NULL
            RETURNING id, label, base_url, key_prefix, created_at, updated_at
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(query, *args)
            return dict(row) if row else None

    async def delete_system_llm_endpoint(self, endpoint_id: str) -> bool:
        """Delete a system endpoint (cascades to model rows)."""
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM llm_endpoints
                WHERE id = $1 AND user_id IS NULL
                """,
                UUID(endpoint_id),
            )
            return result == "DELETE 1"

    # The system endpoint-model accessors (create/update/delete/batch) and
    # resolve_system_llm_model were removed when the admin-curated `models`
    # catalog (below) became the single source of truth for offerings.
    # System endpoints survive as transports; catalog rows reference them
    # via provider_ref.

    # =========================================================================
    # MODELS CATALOG
    # Admin-curated table of (model, capability) offerings anchored to a
    # transport (system_api_keys provider OR system-scoped llm_endpoints row).
    # Reads feed every model picker (builder/session/job) and the resolver's
    # catalog branch in src/core/model_registry.py.
    # =========================================================================

    _MODEL_FIELDS = (
        "id, provider_kind, provider_ref, model_id, display_label, "
        "capabilities, family, context_window, reasoning_level, "
        "params_json, enabled, seeded_from, notes, created_at, updated_at"
    )

    @staticmethod
    def _row_to_model(row: Any) -> Dict[str, Any]:
        """Normalize a models row: parse params_json from JSONB string."""
        d = dict(row)
        params = d.get("params_json")
        if isinstance(params, str):
            d["params_json"] = json.loads(params)
        return d

    @classmethod
    def _canonicalize_capabilities(
        cls,
        *,
        capability: str | None = None,
        capabilities: List[str] | None = None,
    ) -> List[str]:
        """Resolve capability inputs into the canonical ``capabilities[]`` form.

        Accepts either spelling for one release (the singular column is dropped
        in the cleanup chunk):

        - ``capabilities=['chat', 'auxiliary']`` — passed through after
          dedupe + enum validation. Caller chose; we trust it.
        - ``capability='chat'`` — expanded to ``['chat', 'auxiliary']``.
          Reflects the design invariant from
          ``orchestrator/services/readiness.py:16-21``: a chat-capable LLM
          can always run auxiliary tasks. Operators who want a strictly-
          separate auxiliary model still pass ``capabilities=['auxiliary']``.
        - ``capability='embedding'`` (or whisper/tts/vision) — wrapped as
          a singleton ``[capability]``.

        Raises ``ValueError`` if neither is provided or any value is outside
        the locked enum.
        """
        if capabilities is not None:
            caps = list(capabilities)
        elif capability is not None:
            caps = ["chat", "auxiliary"] if capability == "chat" else [capability]
        else:
            raise ValueError(
                "_canonicalize_capabilities requires either capability= or capabilities="
            )
        seen: set[str] = set()
        out: List[str] = []
        for c in caps:
            if not isinstance(c, str):
                raise ValueError(f"capability must be str, got {type(c).__name__}")
            if c not in cls._CATALOG_CAPABILITIES:
                raise ValueError(
                    f"unknown capability {c!r}; allowed: {sorted(cls._CATALOG_CAPABILITIES)}"
                )
            if c not in seen:
                seen.add(c)
                out.append(c)
        if not out:
            raise ValueError("capabilities must be non-empty")
        return out

    async def list_models(
        self,
        *,
        capabilities: List[str] | None = None,
        provider_kind: str | None = None,
        provider_ref: str | None = None,
        enabled_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """List catalog rows with optional filters.

        ``capabilities`` narrows by overlap semantics — a row matches if
        its ``capabilities[]`` contains ANY of the requested values. Pass
        a single-element list (``['chat']``) to filter on one role.
        """
        clauses: list[str] = []
        args: list[Any] = []
        idx = 1
        if capabilities is not None and capabilities:
            clauses.append(f"capabilities && ${idx}::TEXT[]")
            args.append(list(capabilities))
            idx += 1
        if provider_kind is not None:
            clauses.append(f"provider_kind = ${idx}")
            args.append(provider_kind)
            idx += 1
        if provider_ref is not None:
            clauses.append(f"provider_ref = ${idx}")
            args.append(provider_ref)
            idx += 1
        if enabled_only:
            clauses.append("enabled = TRUE")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {self._MODEL_FIELDS} FROM models {where} "
                "ORDER BY provider_kind, provider_ref, display_label",
                *args,
            )
        return [self._row_to_model(r) for r in rows]

    async def get_model(self, model_id: str) -> Dict[str, Any] | None:
        """Fetch a single catalog row by primary key (UUID)."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {self._MODEL_FIELDS} FROM models WHERE id = $1",
                UUID(model_id),
            )
        return self._row_to_model(row) if row else None

    async def create_model(
        self,
        *,
        provider_kind: str,
        provider_ref: str,
        model_id: str,
        display_label: str,
        capability: str | None = None,
        capabilities: List[str] | None = None,
        family: str,
        context_window: int | None = None,
        reasoning_level: str | None = None,
        params_json: Dict[str, Any] | None = None,
        enabled: bool = True,
        seeded_from: str | None = None,
        notes: str | None = None,
        on_conflict_do_nothing: bool = False,
    ) -> Dict[str, Any] | None:
        """Insert a catalog row.

        ``context_window=0`` and ``params_json={"temperature": 0}`` round-trip
        as themselves — only literal ``None`` is treated as "use default"
        (LiteLLM #14661 hazard).

        Capability inputs go through :meth:`_canonicalize_capabilities`.
        ``capability`` (singular) is kept as a kwarg for legacy callers but
        is translated into the canonical ``capabilities[]`` array internally
        — the table only stores the array form.

        When ``on_conflict_do_nothing`` is True and a row already exists for
        ``(provider_kind, provider_ref, model_id)``, returns None so the seed
        pipeline can count "newly inserted" cleanly.
        """
        canonical = self._canonicalize_capabilities(
            capability=capability, capabilities=capabilities
        )
        on_conflict = (
            "ON CONFLICT (provider_kind, provider_ref, model_id) DO NOTHING"
            if on_conflict_do_nothing
            else ""
        )
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO models
                    (provider_kind, provider_ref, model_id, display_label,
                     capabilities, family, context_window,
                     reasoning_level, params_json, enabled, seeded_from, notes)
                VALUES ($1, $2, $3, $4, $5::TEXT[], $6, $7, $8, $9, $10, $11, $12)
                {on_conflict}
                RETURNING {self._MODEL_FIELDS}
                """,
                provider_kind,
                provider_ref,
                model_id,
                display_label,
                canonical,
                family,
                context_window,
                reasoning_level,
                json.dumps(params_json) if params_json is not None else None,
                enabled,
                seeded_from,
                notes,
            )
        return self._row_to_model(row) if row else None

    async def update_model(self, model_id: str, **fields: Any) -> Dict[str, Any] | None:
        """Patch a catalog row. Only keys present in ``fields`` are updated.

        Pass ``context_window=0`` or ``params_json={...}`` to set those values
        explicitly; pass ``None`` to write a SQL NULL (resets to default).
        Use a sentinel-free pattern: only the keys the caller passes are
        considered for the UPDATE.

        ``capability`` (legacy singular) and ``capabilities`` (array) both
        route through ``_canonicalize_capabilities``. The accessor stores
        only the array form.
        """
        allowed = {
            "provider_kind",
            "provider_ref",
            "model_id",
            "display_label",
            "capabilities",
            "family",
            "context_window",
            "reasoning_level",
            "params_json",
            "enabled",
            "notes",
        }
        # Capability changes are coupled — canonicalize singular/array
        # spellings into the array form before writing.
        if "capability" in fields or "capabilities" in fields:
            canonical = self._canonicalize_capabilities(
                capability=fields.pop("capability", None),
                capabilities=fields.pop("capabilities", None),
            )
            fields["capabilities"] = canonical
        sets: list[str] = []
        args: list[Any] = [UUID(model_id)]
        idx = 2
        for name, value in fields.items():
            if name not in allowed:
                continue
            if name == "capabilities":
                sets.append(f"{name} = ${idx}::TEXT[]")
            else:
                sets.append(f"{name} = ${idx}")
            if name == "params_json" and value is not None:
                args.append(json.dumps(value))
            else:
                args.append(value)
            idx += 1
        if not sets:
            return await self.get_model(model_id)
        sets.append("updated_at = CURRENT_TIMESTAMP")
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE models SET {', '.join(sets)} WHERE id = $1 "
                f"RETURNING {self._MODEL_FIELDS}",
                *args,
            )
        return self._row_to_model(row) if row else None

    async def delete_model(self, model_id: str) -> bool:
        """Hard-delete a catalog row. Callers warn on referenced rows."""
        async with self.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM models WHERE id = $1", UUID(model_id)
            )
        return result == "DELETE 1"

    async def resolve_catalog_model(
        self, model_id: str, *, capability: str = "chat"
    ) -> Dict[str, Any] | None:
        """Resolve a catalog row to a flat dict carrying the transport.

        JOINs the models row to its anchor:
        - ``provider_kind='endpoint'`` → ``llm_endpoints`` row supplies
          ``base_url`` and ``api_key`` (decrypted inline).
        - ``provider_kind='system'`` → ``system_api_keys`` row supplies
          ``api_key`` (decrypted inline); ``base_url`` is left None so the
          dispatcher falls through to the provider's hardcoded base URL.

        When multiple rows match (same ``model_id`` under both a system
        provider and an endpoint), the system row wins — direct beats gateway.
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    m.id AS catalog_id,
                    m.provider_kind,
                    m.provider_ref,
                    m.model_id,
                    m.display_label,
                    m.capabilities,
                    m.family,
                    m.context_window,
                    m.reasoning_level,
                    m.params_json,
                    m.enabled,
                    ska.api_key  AS system_api_key,
                    ule.id       AS endpoint_id,
                    ule.label    AS endpoint_label,
                    ule.base_url AS endpoint_base_url,
                    ule.api_key  AS endpoint_api_key
                FROM models m
                LEFT JOIN system_api_keys ska
                    ON m.provider_kind = 'system' AND m.provider_ref = ska.provider
                LEFT JOIN llm_endpoints ule
                    ON m.provider_kind = 'endpoint'
                   AND m.provider_ref = ule.id::text
                   AND ule.user_id IS NULL
                WHERE m.model_id = $1
                  AND $2 = ANY(m.capabilities)
                  AND m.enabled = TRUE
                ORDER BY (m.provider_kind = 'system') DESC, m.created_at ASC
                LIMIT 1
                """,
                model_id,
                capability,
            )
        if row is None:
            return None
        result = self._row_to_model(row)
        if result.get("provider_kind") == "system":
            result["api_key"] = _decrypt_stored(
                result.pop("system_api_key", None),
                field=f"system_api_keys[{result['provider_ref']}]",
            )
            result.pop("endpoint_api_key", None)
        else:
            result["api_key"] = _decrypt_stored(
                result.pop("endpoint_api_key", None),
                field=f"llm_endpoints[{result.get('endpoint_id')}].api_key",
            )
            result.pop("system_api_key", None)
        return result

    async def list_models_by_capability_alphabetical(
        self, capability: str
    ) -> List[Dict[str, Any]]:
        """Enabled catalog rows that include ``capability`` in their
        ``capabilities[]`` array, sorted by display_label.

        Powers the "first-enabled-alphabetical" fallback used by the default-
        model resolver when no admin pin (or a dangling pin) is set. Under
        the array model, one chat row with ``['chat', 'auxiliary']`` is
        considered for both the chat and the auxiliary fallback — the
        operator's intent ("this model serves both roles") is honored.
        """
        async with self.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {self._MODEL_FIELDS} FROM models "
                "WHERE $1 = ANY(capabilities) AND enabled = TRUE "
                "ORDER BY display_label ASC, created_at ASC",
                capability,
            )
        return [self._row_to_model(r) for r in rows]

    async def count_enabled_models_by_capability(self) -> Dict[str, int]:
        """Return ``{capability: count}`` of enabled rows per capability.

        Powers the readiness gate: a capability with zero enabled rows is
        treated as missing. Under the array model, one row contributes to
        every capability in its ``capabilities[]`` array — so a chat row
        seeded as ``['chat', 'auxiliary']`` increments BOTH counts. This
        is exactly what closes the user-reported bug: pinning a chat row
        as the auxiliary default no longer leaves the auxiliary count at
        zero. Capabilities with no rows at all are reported as ``0``
        rather than dropped, so callers can ask for "is `embedding`
        ready?" without first checking presence.
        """
        async with self.acquire() as conn:
            rows = await conn.fetch(
                "SELECT cap, COUNT(*)::INT AS n "
                "FROM models, unnest(capabilities) AS cap "
                "WHERE enabled = TRUE "
                "GROUP BY cap"
            )
        counts: Dict[str, int] = {c: 0 for c in self._CATALOG_CAPABILITIES}
        for row in rows:
            counts[row["cap"]] = int(row["n"])
        return counts

    async def list_default_pin_capabilities(self) -> List[str]:
        """Return the catalog capabilities that have an admin-pinned default.

        Reads `system_settings` keys of the form ``llm.default_<cap>_model``
        and returns the capability portion when the value carries a
        non-empty model ID. Used by the readiness gate to compute
        ``missing_defaults`` without N round-trips.
        """
        prefix = "llm.default_"
        suffix = "_model"
        async with self.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value FROM system_settings "
                "WHERE key LIKE $1 AND key LIKE $2",
                f"{prefix}%",
                f"%{suffix}",
            )
        out: List[str] = []
        for row in rows:
            key = row["key"]
            if not (key.startswith(prefix) and key.endswith(suffix)):
                continue
            capability = key[len(prefix) : -len(suffix)]
            if not capability:
                continue
            value = row["value"]
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except (TypeError, ValueError):
                    value = None
            model: str | None = None
            if isinstance(value, dict):
                model = (
                    value.get("model") if isinstance(value.get("model"), str) else None
                )
            elif isinstance(value, str):
                model = value or None
            if model:
                out.append(capability)
        return out

    # =========================================================================
    # DEFAULT LLM MODEL HELPERS
    # Thin wrappers over the existing system_settings table (section 9d).
    # Keys: llm.default_builder_model / llm.default_browser_model /
    #       llm.default_citation_model
    # Value shape: {"model": "<model_id>"}
    # Replaces the BUILDER_MODEL / BROWSER_LLM_MODEL / CITATION_LLM_MODEL
    # env vars as the source of truth for the default model per workload.
    # =========================================================================

    @staticmethod
    def _default_llm_model_key(kind: str) -> str:
        return f"llm.default_{kind}_model"

    async def get_default_llm_model(self, kind: str) -> str | None:
        """Return the configured default model ID for ``kind`` or None.

        ``kind`` is one of 'builder', 'browser', 'citation'.
        """
        row = await self.get_system_setting(self._default_llm_model_key(kind))
        if row is None:
            return None
        value = row.get("value")
        if isinstance(value, dict):
            model = value.get("model")
            return model if isinstance(model, str) and model else None
        if isinstance(value, str) and value:
            return value
        return None

    async def set_default_llm_model(
        self,
        kind: str,
        model: str | None,
        *,
        updated_by: str | None = None,
    ) -> None:
        """Set or clear the default model ID for ``kind``."""
        key = self._default_llm_model_key(kind)
        if model is None or model == "":
            await self.delete_system_setting(key)
            return
        await self.upsert_system_setting(key, {"model": model}, updated_by=updated_by)

    # Catalog capabilities that support a "first-enabled-alphabetical" fallback
    # when the admin pin is missing or dangling. Whisper/tts gained catalog
    # rows in v1.1; non-catalog kinds (none today) would pass through unchanged.
    _CATALOG_CAPABILITIES = frozenset(
        {"chat", "auxiliary", "embedding", "vision", "whisper", "tts"}
    )

    async def resolve_default_for_capability(self, capability: str) -> str | None:
        """Resolve the effective default model ID for a capability.

        Behavior:
        - Reads the admin pin via ``get_default_llm_model(capability)``.
        - For catalog-supported capabilities, the pin is validated against
          ``resolve_catalog_model``. A pin pointing at a missing or
          ``enabled=false`` row is treated as absent, and the first
          enabled catalog row for the capability (sorted alphabetically by
          ``display_label``) is returned instead.
        - For any capability not in ``_CATALOG_CAPABILITIES`` the pin is
          returned verbatim. As of catalog v1.1 every capability is
          catalog-backed, so this branch is currently a no-op kept for
          forward compat.
        - Returns ``None`` only when no pin exists AND no enabled catalog
          row is available.
        """
        pinned = await self.get_default_llm_model(capability)
        if capability not in self._CATALOG_CAPABILITIES:
            return pinned
        if pinned:
            catalog_row = await self.resolve_catalog_model(
                pinned, capability=capability
            )
            if catalog_row is not None and catalog_row.get("enabled"):
                return pinned
        candidates = await self.list_models_by_capability_alphabetical(capability)
        if candidates:
            return candidates[0]["model_id"]
        return None

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
                       is_admin, can_use_vm, created_at
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
                       is_admin, can_use_vm, keycloak_sub, created_at
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
                       is_admin, can_use_vm, keycloak_sub, created_at
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
                           is_admin, can_use_vm, keycloak_sub, created_at
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
                       is_admin, can_use_vm, keycloak_sub, created_at
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
                                  is_admin, can_use_vm, keycloak_sub, created_at
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
                               is_admin, can_use_vm, keycloak_sub, created_at
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
                       is_admin, can_use_vm, keycloak_sub, created_at
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
                              nextcloud_folder_id, cloud_storage_read_only,
                              main_cloud_backend, main_cloud_folder_handle,
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
        is_admin: bool | None = None,
        can_use_vm: bool | None = None,
    ) -> bool:
        """Update a user.

        Args:
            user_id: User UUID
            display_name: New display name
            avatar_color: New avatar color
            email: New email address
            is_admin: New admin flag (admin-only callers)
            can_use_vm: New per-user VM grant (admin-only callers)

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

        if is_admin is not None:
            param_count += 1
            updates.append(f"is_admin = ${param_count}")
            values.append(bool(is_admin))

        if can_use_vm is not None:
            param_count += 1
            updates.append(f"can_use_vm = ${param_count}")
            values.append(bool(can_use_vm))

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
                          nextcloud_folder_id, cloud_storage_read_only,
                          main_cloud_backend, main_cloud_folder_handle,
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
                       nextcloud_folder_id, cloud_storage_read_only,
                       main_cloud_backend, main_cloud_folder_handle,
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
                       p.nextcloud_folder_id, p.cloud_storage_read_only,
                       p.main_cloud_backend, p.main_cloud_folder_handle,
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
                      default_config_name, default_config_override,
                      nextcloud_folder_id, cloud_storage_read_only)

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
            "nextcloud_folder_id",
            "cloud_storage_read_only",
            "main_cloud_backend",
            "main_cloud_folder_handle",
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
                          nextcloud_folder_id, cloud_storage_read_only,
                          main_cloud_backend, main_cloud_folder_handle,
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
                RETURNING id, job_id, expert_id, user_id, created_at, updated_at, summary, title
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
                SELECT id, job_id, expert_id, user_id, created_at, updated_at,
                       summary, title
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

    async def list_builder_sessions(
        self,
        user_id: str,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """List builder sessions for a user, most recent first."""
        try:
            user_uuid = UUID(user_id)
        except ValueError:
            return []

        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, title, expert_id, created_at, updated_at
                FROM builder_sessions
                WHERE user_id = $1
                ORDER BY updated_at DESC
                LIMIT $2
                """,
                user_uuid,
                limit,
            )

        return [dict(r) for r in rows]

    async def update_builder_session_title(self, session_id: str, title: str) -> bool:
        """Set the auto-generated title for a builder session."""
        try:
            uuid_val = UUID(session_id)
        except ValueError:
            return False

        async with self.acquire() as conn:
            result = await conn.execute(
                "UPDATE builder_sessions SET title = $1 WHERE id = $2",
                title,
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

    async def apply_migrations(self) -> bool:
        """Apply pending migrations from this instance's migrations directory.

        Thin wrapper over ``orchestrator.database.migrate.run_migrations`` —
        each ``PostgresDB`` instance binds to its migrations dir at
        construction time, so the call site doesn't pass the directory.
        See ``docs/db_migration.md`` for the runner's design and contract.

        Returns:
            True if migrations were applied successfully.

        Raises:
            RuntimeError: If not connected, or migrations dir is invalid,
                or a previous run left a dirty row, or checksum drift was
                detected.
        """
        try:
            # Host-side: invoked from repo root (e.g. `python init.py`) where
            # the orchestrator package is importable via its full path.
            from orchestrator.database.migrate import run_migrations
        except ImportError:
            # In-container: Dockerfile.orchestrator copies orchestrator/ flat
            # into /app with PYTHONPATH=/app, so the same module is reachable
            # as a top-level `database` package.
            from database.migrate import run_migrations

        if self._pool is None:
            raise RuntimeError("apply_migrations() called before connect()")

        await run_migrations(self._pool, self._migrations_dir)
        logger.info("Applied migrations from %s", self._migrations_dir)

        # Data migration only applies to the app DB. This predates the
        # numbered-migrations system and will be folded into a real
        # migration file the next time it gets touched.
        if self._migrations_dir == MIGRATIONS_APP_DIR:
            await self.migrate_existing_users_verified()

        return True

    async def reset_schema(self) -> None:
        """Drop all tables and re-apply migrations from 0001 onward.

        WARNING: This deletes all data.

        Drops the public schema entirely, recreates it, then runs the
        full migration chain on a fresh DB.

        Raises:
            RuntimeError: If not connected to database.
        """
        async with self.acquire() as conn:
            # Nuclear option: drop and recreate public schema
            await conn.execute("DROP SCHEMA public CASCADE")
            await conn.execute("CREATE SCHEMA public")
            await conn.execute("GRANT ALL ON SCHEMA public TO public")
            logger.info("Dropped all tables (schema reset)")

        # Re-apply fresh chain. The runner re-creates schema_migrations
        # (it was dropped with the public schema) and applies every migration.
        await self.apply_migrations()

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
    # SYSTEM SETTINGS (Phase 4)
    # =========================================================================
    # Key/value store for deploy-time configuration that operators can edit
    # via the cockpit admin UI. Secrets never land here — the credentials_ref
    # column carries a pointer (e.g. "env:OPENCLOUD_KEYCLOAK_CLIENT_SECRET")
    # that the reader resolves against its own secret store.

    async def get_system_setting(self, key: str) -> Dict[str, Any] | None:
        """Return the full system_settings row for ``key`` or None."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT key, value, credentials_ref, updated_at, updated_by
                FROM system_settings WHERE key = $1
                """,
                key,
            )
        if row is None:
            return None
        d = self._row_to_dict(row) or {}
        raw_value = d.get("value")
        if isinstance(raw_value, str):
            try:
                d["value"] = json.loads(raw_value)
            except (ValueError, TypeError):
                d["value"] = {}
        return d

    async def upsert_system_setting(
        self,
        key: str,
        value: Dict[str, Any],
        *,
        credentials_ref: Optional[str] = None,
        updated_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create or replace a system_settings row.

        Returns the post-write row (after the DB-side updated_at is set).
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO system_settings
                    (key, value, credentials_ref, updated_at, updated_by)
                VALUES ($1, $2::jsonb, $3, CURRENT_TIMESTAMP, $4)
                ON CONFLICT (key) DO UPDATE SET
                    value = EXCLUDED.value,
                    credentials_ref = EXCLUDED.credentials_ref,
                    updated_at = CURRENT_TIMESTAMP,
                    updated_by = EXCLUDED.updated_by
                RETURNING key, value, credentials_ref, updated_at, updated_by
                """,
                key,
                json.dumps(value),
                credentials_ref,
                updated_by,
            )
        d = self._row_to_dict(row) or {}
        raw_value = d.get("value")
        if isinstance(raw_value, str):
            try:
                d["value"] = json.loads(raw_value)
            except (ValueError, TypeError):
                d["value"] = {}
        return d

    async def delete_system_setting(self, key: str) -> bool:
        """Remove a system_settings row. Returns True if a row was deleted."""
        async with self.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM system_settings WHERE key = $1",
                key,
            )
        return result.endswith(" 1")

    async def notify_channel(self, channel: str, payload: str = "") -> None:
        """Fire a Postgres NOTIFY on ``channel``.

        Used by Phase 4 to broadcast config changes to every orchestrator
        replica via the LISTEN task registered at startup. The payload is
        a free-form string — callers that need structured data should
        JSON-encode it themselves.

        Uses ``SELECT pg_notify($1, $2)`` rather than the bare ``NOTIFY``
        statement because bare NOTIFY only accepts literal payloads,
        while ``pg_notify`` is a regular SQL function and honors
        parameterized arguments — safe against channel-injection and
        free-form payloads with quotes.
        """
        if not channel.replace("_", "").isalnum():
            raise ValueError(f"invalid NOTIFY channel: {channel!r}")
        async with self.acquire() as conn:
            await conn.execute("SELECT pg_notify($1, $2)", channel, payload)

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
