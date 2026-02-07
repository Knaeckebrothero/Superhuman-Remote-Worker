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
import math
import os
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Any, List, Dict
from contextlib import asynccontextmanager
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
ALLOWED_TABLES = frozenset({"jobs", "agents", "requirements", "sources", "citations"})

# Required tables that must exist for the orchestrator to function
REQUIRED_TABLES = ["jobs", "agents", "requirements", "sources", "citations"]

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
            "postgresql://graphrag:graphrag_password@localhost:5432/graphrag",
        )

        self._min_connections = min_connections or int(os.getenv("POSTGRES_MIN_CONNECTIONS", "2"))
        self._max_connections = max_connections or int(os.getenv("POSTGRES_MAX_CONNECTIONS", "10"))
        self._command_timeout = command_timeout or 60.0

        self._pool: Optional[asyncpg.Pool] = None
        self._queries: Dict[str, str] = {}  # Cache for loaded queries

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
            if table_name in ("jobs", "requirements", "citations"):
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
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Get list of jobs with optional status filter.

        Args:
            status: Optional status filter (e.g., 'completed', 'processing')
            limit: Maximum number of jobs to return

        Returns:
            List of job dicts with id, description, status, creator_status, validator_status, created_at
        """
        async with self.acquire() as conn:
            if status:
                rows = await conn.fetch(
                    """
                    SELECT id, description, status, creator_status, validator_status,
                           config_name, assigned_agent_id, created_at
                    FROM jobs
                    WHERE status = $1
                    ORDER BY created_at DESC
                    LIMIT $2
                    """,
                    status,
                    limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, description, status, creator_status, validator_status,
                           config_name, assigned_agent_id, created_at
                    FROM jobs
                    ORDER BY created_at DESC
                    LIMIT $1
                    """,
                    limit,
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
                SELECT id, status, creator_status, validator_status,
                       config_name, config_override, assigned_agent_id,
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
    ) -> Dict[str, Any]:
        """Create a new job.

        Args:
            description: Job description - what the agent should accomplish
            document_path: Optional path to a document
            document_dir: Optional directory containing documents
            config_name: Agent configuration name (default: "default")
            config_override: Optional per-job configuration overrides
            context: Optional context dictionary

        Returns:
            Created job dict with id
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO jobs (description, document_path, config_name, config_override, context, status, creator_status, validator_status)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING id, status, creator_status, validator_status, config_name, assigned_agent_id, created_at, updated_at, description
                """,
                description,
                document_path or document_dir,
                config_name,
                json.dumps(config_override) if config_override else None,
                json.dumps(context) if context else None,
                "created",
                "pending",
                "pending",
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
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = $1 AND status NOT IN ('completed', 'cancelled')
                """,
                uuid_val,
            )

        return result == "UPDATE 1"

    async def update_job_status(
        self,
        job_id: str,
        status: str | None = None,
        creator_status: str | None = None,
        validator_status: str | None = None,
        assigned_agent_id: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        """Update job status fields.

        Args:
            job_id: Job UUID as string
            status: New job status
            creator_status: New creator status
            validator_status: New validator status
            assigned_agent_id: Agent ID if being assigned
            error_message: Error message if failed

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

        if creator_status is not None:
            param_count += 1
            updates.append(f"creator_status = ${param_count}")
            values.append(creator_status)

        if validator_status is not None:
            param_count += 1
            updates.append(f"validator_status = ${param_count}")
            values.append(validator_status)

        if error_message is not None:
            param_count += 1
            updates.append(f"error_message = ${param_count}")
            values.append(error_message)

        if assigned_agent_id is not None:
            param_count += 1
            updates.append(f"assigned_agent_id = ${param_count}")
            values.append(UUID(assigned_agent_id) if assigned_agent_id else None)

        if not updates:
            return False

        updates.append("updated_at = CURRENT_TIMESTAMP")
        param_count += 1
        values.append(uuid_val)

        query = f"UPDATE jobs SET {', '.join(updates)} WHERE id = ${param_count}"

        async with self.acquire() as conn:
            result = await conn.execute(query, *values)

        return result == "UPDATE 1"

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

        query = "UPDATE jobs SET context = $1, updated_at = CURRENT_TIMESTAMP WHERE id = $2"
        async with self.acquire() as conn:
            result = await conn.execute(query, json_module.dumps(context), uuid_val)

        return result == "UPDATE 1"

    async def get_requirements(
        self,
        job_id: str,
        status: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Get requirements for a job with optional filtering.

        Args:
            job_id: Job UUID as string
            status: Optional status filter
            limit: Maximum requirements to return
            offset: Number to skip

        Returns:
            Dict with requirements list and total count
        """
        try:
            uuid_val = UUID(job_id)
        except ValueError:
            return {"requirements": [], "total": 0}

        async with self.acquire() as conn:
            # Get total count
            if status:
                count_row = await conn.fetchrow(
                    "SELECT COUNT(*) as total FROM requirements WHERE job_id = $1 AND status = $2",
                    uuid_val,
                    status,
                )
                rows = await conn.fetch(
                    """
                    SELECT id, requirement_id, name, text, type, priority, status,
                           source_document, gobd_relevant, gdpr_relevant,
                           quality_score, fulfillment_status, neo4j_id,
                           created_at, updated_at
                    FROM requirements
                    WHERE job_id = $1 AND status = $2
                    ORDER BY created_at
                    LIMIT $3 OFFSET $4
                    """,
                    uuid_val,
                    status,
                    limit,
                    offset,
                )
            else:
                count_row = await conn.fetchrow(
                    "SELECT COUNT(*) as total FROM requirements WHERE job_id = $1",
                    uuid_val,
                )
                rows = await conn.fetch(
                    """
                    SELECT id, requirement_id, name, text, type, priority, status,
                           source_document, gobd_relevant, gdpr_relevant,
                           quality_score, fulfillment_status, neo4j_id,
                           created_at, updated_at
                    FROM requirements
                    WHERE job_id = $1
                    ORDER BY created_at
                    LIMIT $2 OFFSET $3
                    """,
                    uuid_val,
                    limit,
                    offset,
                )

        return {
            "requirements": [dict(row) for row in rows],
            "total": count_row["total"] if count_row else 0,
            "limit": limit,
            "offset": offset,
        }

    async def get_requirement_summary(self, job_id: str) -> Dict[str, int]:
        """Get requirement counts by status for a job.

        Args:
            job_id: Job UUID as string

        Returns:
            Dict with counts by status
        """
        try:
            uuid_val = UUID(job_id)
        except ValueError:
            return {"pending": 0, "validating": 0, "integrated": 0, "rejected": 0, "failed": 0, "total": 0}

        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (WHERE status = 'pending') as pending,
                    COUNT(*) FILTER (WHERE status = 'validating') as validating,
                    COUNT(*) FILTER (WHERE status = 'integrated') as integrated,
                    COUNT(*) FILTER (WHERE status = 'rejected') as rejected,
                    COUNT(*) FILTER (WHERE status = 'failed') as failed,
                    COUNT(*) as total
                FROM requirements
                WHERE job_id = $1
                """,
                uuid_val,
            )

        if row:
            return dict(row)
        return {"pending": 0, "validating": 0, "integrated": 0, "rejected": 0, "failed": 0, "total": 0}

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
            # Get job info
            job = await conn.fetchrow(
                """
                SELECT id, description, status, creator_status, validator_status,
                       config_name, assigned_agent_id, created_at, updated_at, completed_at
                FROM jobs WHERE id = $1
                """,
                uuid_val,
            )
            if not job:
                return None

            # Get requirement counts
            req_counts = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE status = 'pending') as pending,
                    COUNT(*) FILTER (WHERE status = 'validating') as validating,
                    COUNT(*) FILTER (WHERE status = 'integrated') as integrated,
                    COUNT(*) FILTER (WHERE status = 'rejected') as rejected,
                    COUNT(*) FILTER (WHERE status = 'failed') as failed
                FROM requirements WHERE job_id = $1
                """,
                uuid_val,
            )

        # Calculate progress
        total = req_counts["total"]
        processed = req_counts["integrated"] + req_counts["rejected"]
        progress_percent = (processed / total * 100) if total > 0 else 0

        # Calculate ETA
        created_at = job["created_at"]
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        elapsed = now - created_at
        elapsed_seconds = elapsed.total_seconds()

        eta_seconds = None
        if processed > 0 and total > processed:
            avg_time_per_req = elapsed_seconds / processed
            remaining = (total - processed) * avg_time_per_req
            eta_seconds = remaining

        return {
            "job_id": str(job["id"]),
            "status": job["status"],
            "creator_status": job["creator_status"],
            "validator_status": job["validator_status"],
            "requirements": dict(req_counts),
            "progress_percent": round(progress_percent, 1),
            "elapsed_seconds": elapsed_seconds,
            "eta_seconds": eta_seconds,
            "created_at": job["created_at"].isoformat() if job["created_at"] else None,
            "updated_at": job["updated_at"].isoformat() if job["updated_at"] else None,
            "completed_at": job["completed_at"].isoformat() if job["completed_at"] else None,
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

    async def detect_stuck_jobs(self, threshold_minutes: int = 60) -> List[Dict[str, Any]]:
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
                SELECT j.id, j.description, j.status, j.creator_status, j.validator_status,
                       j.config_name, j.assigned_agent_id, j.created_at, j.updated_at,
                       COUNT(r.id) FILTER (WHERE r.status = 'pending') as pending_requirements,
                       COUNT(r.id) FILTER (WHERE r.status = 'integrated') as integrated_requirements
                FROM jobs j
                LEFT JOIN requirements r ON j.id = r.job_id
                WHERE j.status = 'processing'
                AND j.updated_at < $1
                GROUP BY j.id
                ORDER BY j.updated_at ASC
                """,
                threshold,
            )

        stuck_jobs = []
        for row in rows:
            job = dict(row)
            # Determine stuck reason
            if job["creator_status"] == "processing" and job["integrated_requirements"] == 0:
                job["stuck_reason"] = "Creator not producing requirements"
                job["stuck_component"] = "creator"
            elif job["creator_status"] == "completed" and job["pending_requirements"] > 0:
                job["stuck_reason"] = f"Validator not processing {job['pending_requirements']} pending requirements"
                job["stuck_component"] = "validator"
            else:
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
                    # Update existing agent's IP and reset status
                    await conn.execute(
                        """
                        UPDATE agents
                        SET pod_ip = $1,
                            pod_port = $2,
                            pid = $3,
                            config_name = $4,
                            status = 'booting',
                            last_heartbeat = CURRENT_TIMESTAMP,
                            registered_at = CURRENT_TIMESTAMP
                        WHERE id = $5
                        """,
                        pod_ip,
                        pod_port,
                        pid,
                        config_name,
                        existing["id"],
                    )
                    return {
                        "agent_id": str(existing["id"]),
                        "heartbeat_interval_seconds": 60,
                    }

            # Create new agent
            row = await conn.fetchrow(
                """
                INSERT INTO agents (config_name, hostname, pod_ip, pod_port, pid)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
                """,
                config_name,
                hostname,
                pod_ip,
                pod_port,
                pid,
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
    ) -> bool:
        """Update agent heartbeat and status.

        Args:
            agent_id: Agent UUID
            status: Agent status (booting, ready, working, completed, failed)
            current_job_id: Optional current job UUID
            metrics: Optional metrics dict to merge into metadata

        Returns:
            True if update successful, False if agent not found
        """
        try:
            uuid_val = UUID(agent_id)
        except ValueError:
            return False

        job_uuid = UUID(current_job_id) if current_job_id else None

        async with self.acquire() as conn:
            if metrics:
                # Merge metrics into metadata
                result = await conn.execute(
                    """
                    UPDATE agents
                    SET status = $1,
                        current_job_id = $2,
                        last_heartbeat = CURRENT_TIMESTAMP,
                        metadata = metadata || $3::jsonb
                    WHERE id = $4
                    """,
                    status,
                    job_uuid,
                    json.dumps(metrics),
                    uuid_val,
                )
            else:
                result = await conn.execute(
                    """
                    UPDATE agents
                    SET status = $1,
                        current_job_id = $2,
                        last_heartbeat = CURRENT_TIMESTAMP
                    WHERE id = $3
                    """,
                    status,
                    job_uuid,
                    uuid_val,
                )

            return result == "UPDATE 1"

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
                           status, current_job_id, registered_at, last_heartbeat, metadata
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
                           status, current_job_id, registered_at, last_heartbeat, metadata
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

    async def get_ready_agents(self) -> List[Dict[str, Any]]:
        """Get all agents with 'ready' status.

        Returns:
            List of ready agent dicts
        """
        return await self.list_agents(status="ready")

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
        db_name = self._connection_string.rsplit('/', 1)[-1].split('?')[0]
        if not db_name:
            raise RuntimeError("Could not extract database name from connection string")

        # Connect to postgres database to create the target database
        base_conn_str = self._connection_string.rsplit('/', 1)[0] + '/postgres'

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

    async def ensure_schema(self) -> bool:
        """Apply schema.sql to initialize database tables.

        This is idempotent - uses IF NOT EXISTS clauses.
        Requires an active connection pool (call connect() first).

        Returns:
            True if schema was applied successfully.

        Raises:
            RuntimeError: If not connected to database.
            FileNotFoundError: If schema.sql doesn't exist.
        """
        if not SCHEMA_FILE.exists():
            raise FileNotFoundError(f"Schema file not found: {SCHEMA_FILE}")

        schema_sql = SCHEMA_FILE.read_text()

        async with self.acquire() as conn:
            await conn.execute(schema_sql)

        logger.info(f"Applied schema from {SCHEMA_FILE}")
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


__all__ = ['PostgresDB', 'ALLOWED_TABLES', 'PG_TYPE_MAP', 'SCHEMA_FILE', 'REQUIRED_TABLES']
