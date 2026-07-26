"""PostgreSQL Database Manager with async connection pooling.

This module provides the canonical async PostgreSQL interface using asyncpg with:
- Async connection pooling
- Query execution methods (execute, fetch, fetchrow, fetchval)
- Named query loading from SQL files
- Job, agent, and requirement management
- Sync wrappers for non-async contexts

This is the canonical database layer for the orchestrator.
"""

import hashlib
import json
import logging
import math
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Any, List, Dict, Tuple
from uuid import UUID, uuid4

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

# Non-lifecycle workspace metadata which may be written independently while a
# static Docker lease is allocated. Allocation replaces every authority-bearing
# endpoint/lifecycle field, but must not erase repository attachment or the
# documented snapshot reference/work marker used to restore durable state.
_DOCKER_WORKSPACE_PRESERVED_FIELDS = frozenset(
    {"git_remote_url", "repo_name", "last_snapshot", "last_snapshot_turns"}
)
_DOCKER_WORKSPACE_TRANSITION_PRESERVED_FIELDS = _DOCKER_WORKSPACE_PRESERVED_FIELDS | {
    "ide_host",
    "ide_port",
}
_DOCKER_WORKSPACE_GENERATION_KEY = "_canvas_workspace_generation"
_DOCKER_WORKSPACE_LEASE_KEY = "_docker_workspace_lease_id"
_DOCKER_WORKSPACE_TRUST_KEY = "_docker_workspace_trust_mode"
_DOCKER_WORKSPACE_ATTESTED_KEY = "_docker_workspace_attested"
_DOCKER_INVENTORY_COLUMNS = (
    "host, port, status, lease_id, owner_kind, owner_id, trust_mode, "
    "host_key_fingerprint, quarantine_reason"
)


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


def _encrypt_credentials_dict(creds: Dict[str, Any] | None) -> str:
    """Encrypt a credentials dict for insertion into a JSONB column.

    Returns a JSON string ready to pass as a JSONB parameter. Empty/None
    becomes JSONB ``'{}'`` (no encryption needed — empty has no secret to
    protect, and the schema default is ``'{}'``).

    A non-empty dict is JSON-serialized, encrypted with :func:`encrypt`,
    then wrapped again with :func:`json.dumps` so the JSONB column receives
    a valid JSON string value (e.g. ``"v1:<nonce>:<ct>"``).
    """
    if not creds:
        return "{}"
    return json.dumps(encrypt(json.dumps(creds)))


def _decrypt_credentials_field(
    raw: Any, *, field: str = "datasources.credentials"
) -> Dict[str, Any]:
    """Decrypt a credentials JSONB value, returning the plaintext dict.

    asyncpg returns JSONB as a raw JSON string here (no codec registered).
    This helper parses the JSON and handles three cases:

    1. **Legacy plaintext** — JSON object (pre-encryption rows). Returned
       as-is with a warning so operators can re-save to upgrade.
    2. **Encrypted** — JSON string with ``v1:`` prefix. Decrypted and
       JSON-parsed back into a dict.
    3. **Other / malformed** — empty dict, logged.

    Always returns a dict (possibly empty) so callers never have to handle
    None, string, or dict variants.
    """
    if raw is None:
        return {}

    if isinstance(raw, str):
        try:
            parsed: Any = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.error("Failed to parse %s JSON: %s", field, exc)
            return {}
    else:
        parsed = raw  # asyncpg with a JSON codec would land here

    if isinstance(parsed, dict):
        if parsed:
            logger.warning(
                "Legacy plaintext credentials in %s; re-save the datasource to encrypt.",
                field,
            )
        return parsed

    if isinstance(parsed, str):
        if not is_encrypted(parsed):
            logger.warning(
                "Non-encrypted credential string in %s; treating as empty.", field
            )
            return {}
        try:
            plaintext = decrypt(parsed)
            return json.loads(plaintext)
        except (DecryptionError, json.JSONDecodeError, ValueError) as exc:
            logger.error("Failed to decrypt %s: %s", field, exc)
            return {}

    logger.warning(
        "Unexpected credentials type %s in %s; treating as empty.",
        type(parsed).__name__,
        field,
    )
    return {}


def _datasource_row_to_dict(
    row, *, field: str = "datasources.credentials"
) -> Dict[str, Any]:
    """Convert a datasource asyncpg Record to a dict with decrypted credentials.

    Use anywhere a SELECT returns a datasource row. The ``credentials`` field
    is replaced with its decrypted plaintext dict (always a dict, never the
    raw JSONB string).
    """
    d = dict(row)
    if "credentials" in d:
        d["credentials"] = _decrypt_credentials_field(d["credentials"], field=field)
    if "config" in d:
        raw_config = d["config"]
        if isinstance(raw_config, str):
            try:
                raw_config = json.loads(raw_config)
            except (json.JSONDecodeError, ValueError) as exc:
                logger.error("Failed to parse datasources.config JSON: %s", exc)
                raw_config = {}
        if raw_config is None:
            raw_config = {}
        if not isinstance(raw_config, dict):
            logger.warning(
                "Unexpected datasources.config type %s; treating as empty.",
                type(raw_config).__name__,
            )
            raw_config = {}
        d["config"] = raw_config
    return d


QUERIES_DIR = Path(__file__).parent / "queries" / "postgres"

# Frozen schema reference (no longer applied at runtime — see migrate.py).
SCHEMA_FILE = Path(__file__).parent / "schema.sql"

# Migration directories per DB. The runner picks one of these via the
# ``migrations_dir`` constructor kwarg on ``PostgresDB``; lifespan + init
# wire each instance to its own subdir.
MIGRATIONS_APP_DIR = Path(__file__).parent / "migrations" / "app"

# --- Job execution lease (docs/features/job_execution_lease.md) -------------
# Pickup lease: set atomically by claim_job_for_agent, covers the dispatch
# POST + agent init + workspace connect. If the pod is dead or never accepts,
# nothing renews and the job recovers by expiry — this is what makes the
# dispatcher's claim-then-notify-without-rollback design sound.
JOB_LEASE_PICKUP_SECONDS = 180
# Run lease: renewed by the agent's 5s heartbeats (18 missed beats of slack).
JOB_LEASE_RUN_SECONDS = 90
# Renewal write throttle: skip the UPDATE while more than this much lease
# remains, so the 5s heartbeat doesn't churn the row (trigger bumps
# updated_at + the partial index makes lease writes non-HOT). Effective
# renewal write rate: about one per (RUN - THROTTLE) = 30s per running job.
JOB_LEASE_RENEW_BELOW_SECONDS = 60
MIGRATIONS_VECTOR_DIR = Path(__file__).parent / "migrations" / "vector"
MIGRATIONS_AUDIT_DIR = Path(__file__).parent / "migrations" / "audit"

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
    "projects",
    "project_members",
    "project_repositories",
    "jobs",
    "agents",
    "datasources",
    "user_api_keys",
    "project_api_keys",
    "models",
    "experts",
    "application_expert_defaults",
    "user_expert_defaults",
    "expert_default_audit",
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
        env_prefix: str = "POSTGRES",
        default_min_connections: int = 2,
        default_max_connections: int = 10,
    ):
        """Initialize PostgreSQL database manager.

        Args:
            connection_string: PostgreSQL connection URL. Falls back to DATABASE_URL env var.
            min_connections: Minimum pool size. Falls back to
                ``{env_prefix}_MIN_CONNECTIONS`` then ``default_min_connections``.
            max_connections: Maximum pool size. Falls back to
                ``{env_prefix}_MAX_CONNECTIONS`` then ``default_max_connections``.
            command_timeout: Query timeout in seconds (default: 60.0)
            migrations_dir: Directory of NNNN_*.sql migrations applied by
                ``apply_migrations``. Defaults to ``migrations/app/``; the
                vector instance overrides this to ``migrations/vector/``.
            env_prefix: Env-var prefix for this store's pool + DSN
                (``POSTGRES`` control plane, ``VECTOR_POSTGRES``,
                ``AUDIT_POSTGRES``). Each store reads its OWN
                ``{env_prefix}_MIN/MAX_CONNECTIONS`` so the vector/audit pools no
                longer silently inherit the control-plane ``POSTGRES_*`` sizing.
            default_min_connections: Baked-in min when the env var is unset
                (control 2, vector 1, audit 1) — never falls through to
                asyncpg's own ``create_pool`` default (min 10).
            default_max_connections: Baked-in max when the env var is unset
                (control 10, vector 5, audit 4).

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
                env_prefix,
                fallback_env="DATABASE_URL",
            )
            or "postgresql://srw:srw_password@localhost:5432/srw"
        )

        self._min_connections = min_connections or int(
            os.getenv(f"{env_prefix}_MIN_CONNECTIONS", str(default_min_connections))
        )
        self._max_connections = max_connections or int(
            os.getenv(f"{env_prefix}_MAX_CONNECTIONS", str(default_max_connections))
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

    @property
    def pool(self) -> Optional[asyncpg.Pool]:
        """The underlying asyncpg pool (``None`` until ``connect()``).

        Exposed for background tasks that need the raw pool rather than the
        higher-level helpers — e.g. the audit partition-maintenance loop,
        which acquires its own connections for DDL (ATTACH/DETACH/ANALYZE)
        and catalog introspection.
        """
        return self._pool

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
            conditions.append(f"j.status = ${param_count}")
            values.append(status)

        if user_id:
            param_count += 1
            conditions.append(f"j.user_id = ${param_count}")
            values.append(UUID(user_id))

        if scope_project_id:
            param_count += 1
            conditions.append(f"j.project_id = ${param_count}")
            values.append(UUID(scope_project_id))

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        param_count += 1
        values.append(limit)

        async with self.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT j.id, j.description, j.status,
                       j.config_name, j.assigned_agent_id, j.user_id,
                       j.project_id, j.parent_job_id, j.priority,
                       j.repo_name, j.branch_name, j.merge_status,
                       j.diff_status, j.exported_at, j.error_message,
                       j.created_at, j.created_by_thread_id,
                       j.context->'snapshot'->>'status' AS snapshot_status,
                       p.name AS project_name,
                       (p.main_cloud_folder_handle IS NOT NULL) AS project_has_cloud_folder,
                       (pa.id IS NOT NULL) AS pending_approval,
                       pa.id AS pending_approval_request_id
                FROM jobs j
                LEFT JOIN projects p ON p.id = j.project_id
                LEFT JOIN LATERAL (
                    SELECT s.id FROM sudo_approval_requests s
                    WHERE s.job_id = j.id AND s.status = 'pending'
                      AND s.expires_at > NOW()
                    ORDER BY s.requested_at DESC
                    LIMIT 1
                ) pa ON TRUE
                {where_clause}
                ORDER BY j.created_at DESC
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
            conditions.append(f"j.status = ${param_count}")
            values.append(status)

        param_count += 1
        user_idx = param_count
        param_count += 1
        projects_idx = param_count
        conditions.append(
            f"(j.user_id = ${user_idx} OR j.project_id = ANY(${projects_idx}::uuid[]))"
        )
        values.append(UUID(owner_user_id))
        values.append([UUID(p) for p in visible_project_ids])

        if scope_project_id:
            param_count += 1
            conditions.append(f"j.project_id = ${param_count}")
            values.append(UUID(scope_project_id))

        where_clause = f"WHERE {' AND '.join(conditions)}"
        param_count += 1
        values.append(limit)

        async with self.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT j.id, j.description, j.status,
                       j.config_name, j.assigned_agent_id, j.user_id,
                       j.project_id, j.parent_job_id, j.priority,
                       j.repo_name, j.branch_name, j.merge_status,
                       j.diff_status, j.exported_at, j.error_message,
                       j.created_at, j.created_by_thread_id,
                       j.context->'snapshot'->>'status' AS snapshot_status,
                       p.name AS project_name,
                       (p.main_cloud_folder_handle IS NOT NULL) AS project_has_cloud_folder,
                       (pa.id IS NOT NULL) AS pending_approval,
                       pa.id AS pending_approval_request_id
                FROM jobs j
                LEFT JOIN projects p ON p.id = j.project_id
                LEFT JOIN LATERAL (
                    SELECT s.id FROM sudo_approval_requests s
                    WHERE s.job_id = j.id AND s.status = 'pending'
                      AND s.expires_at > NOW()
                    ORDER BY s.requested_at DESC
                    LIMIT 1
                ) pa ON TRUE
                {where_clause}
                ORDER BY j.created_at DESC
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
                SELECT j.id, j.status,
                       j.config_name, j.expert_id, j.config_override, j.resolved_config,
                       j.assigned_agent_id, j.user_id,
                       j.project_id, j.parent_job_id, j.priority,
                       j.branch_name, j.repo_name, j.merge_status, j.repo_merge_statuses,
                       j.freeze_data,
                       j.cloud_diff_baseline_commit, j.diff_status,
                       j.exported_folder_handle, j.exported_at,
                       j.creation_order, j.worktree_path, j.delegation_context,
                       j.error_message, j.error_details, j.runner_kind,
                       j.created_by_thread_id, j.wake_on_complete,
                       j.created_at, j.updated_at, j.description, j.context,
                       (p.main_cloud_folder_handle IS NOT NULL) AS project_has_cloud_folder
                FROM jobs j
                LEFT JOIN projects p ON p.id = j.project_id
                WHERE j.id = $1
                """,
                uuid_val,
            )

        return dict(row) if row else None

    async def create_job(
        self,
        description: str,
        document_path: str | None = None,
        document_dir: str | None = None,
        config_name: str = "worker_base",
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
        expert_id: str | None = None,
        runner_kind: str = "user",
        status: str = "created",
        freeze_data: Dict[str, Any] | None = None,
        created_by_thread_id: str | None = None,
        wake_on_complete: bool = False,
    ) -> Dict[str, Any]:
        """Create a new job.

        Args:
            description: Job description - what the agent should accomplish
            document_path: Optional path to a document
            document_dir: Optional directory containing documents
            config_name: Agent configuration name (default: "worker_base")
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
            status: Initial status (default "created"; "paused" + freeze_data
                creates a born-parked row the dispatcher cannot pick up)
            freeze_data: Optional freeze dict set atomically at creation — an
                INSERT-time park has no window in which the dispatcher poll
                could grab the row (docs/issues/loop_advances_into_active_model_cooldown.md)
            created_by_thread_id: Session thread that created this job. Only the
                creating *session* sets this — scholar/critic/delegation subjob
                call sites pass parent_job_id alone and therefore correctly do
                NOT inherit the backref (a subjob's completion is the parent
                job's business, not the session's).
            wake_on_complete: Whether this job's terminal state owes the creating
                session a wake. Set server-side by the create endpoint, never by
                the model — see docs/features/session_wake_on_job_completion.md.

        Returns:
            Created job dict with id
        """
        user_uuid = UUID(user_id) if user_id else None
        project_uuid = UUID(project_id) if project_id else None
        parent_uuid = UUID(parent_job_id) if parent_job_id else None
        thread_uuid = UUID(created_by_thread_id) if created_by_thread_id else None

        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO jobs (description, document_path, config_name, config_override, context, status, user_id, project_id, branch_name, parent_job_id, priority, repo_name, creation_order, worktree_path, delegation_context, expert_id, runner_kind, freeze_data, created_by_thread_id, wake_on_complete)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20)
                RETURNING id, status, config_name, assigned_agent_id, user_id, project_id, parent_job_id, priority, branch_name, repo_name, created_at, updated_at, description, creation_order, worktree_path, expert_id, runner_kind, created_by_thread_id, wake_on_complete
                """,
                description,
                document_path or document_dir,
                config_name,
                json.dumps(config_override) if config_override else None,
                json.dumps(context) if context else None,
                status,
                user_uuid,
                project_uuid,
                branch_name,
                parent_uuid,
                priority,
                repo_name,
                creation_order,
                worktree_path,
                delegation_context,
                UUID(expert_id) if expert_id else None,
                runner_kind,
                json.dumps(freeze_data) if freeze_data else None,
                thread_uuid,
                wake_on_complete,
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
            async with conn.transaction():
                # Static Docker inventory deliberately has no owner FK. Keep an
                # in-flight endpoint occupied even if a cleanup caller deletes
                # the owner after a failed/partial release.
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    "srw-docker-workspace-pool",
                )
                await conn.execute(
                    """
                    UPDATE docker_workspace_leases
                    SET status = 'quarantined',
                        quarantine_reason = 'owner_deleted_before_recreation',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE owner_kind = 'job' AND owner_id = $1
                      AND status IN ('ready', 'releasing')
                    """,
                    uuid_val,
                )
                result = await conn.execute(
                    "DELETE FROM jobs WHERE id = $1",
                    uuid_val,
                )

        return result == "DELETE 1"

    async def has_child_jobs(self, job_id: str) -> bool:
        """True if any job row (any status) has this job as its parent.

        Mirrors the ``parent_job_id`` FK exactly: such a parent cannot be
        row-deleted until its descendants are gone.
        """
        try:
            uuid_val = UUID(job_id)
        except ValueError:
            return False

        async with self.acquire() as conn:
            return bool(
                await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM jobs WHERE parent_job_id = $1)",
                    uuid_val,
                )
            )

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

        cancelled = result == "UPDATE 1"
        if cancelled:
            # Terminal → prune the LangGraph checkpoint (no-op on sqlite backend).
            try:
                await self.delete_checkpoint_thread(job_id)
            except Exception as e:
                logger.debug("checkpoint prune skipped for %s: %s", job_id, e)
        return cancelled

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
        error_details: Dict[str, Any] | None = None,
    ) -> bool:
        """Update job status fields.

        Args:
            job_id: Job UUID as string
            status: New job status
            assigned_agent_id: Agent ID if being assigned
            error_message: Error message if failed
            freeze_data: Freeze metadata dict (for waiting_for_reply, pending_review)
            error_details: Structured agent error (classification/model/reset_at)
                persisted alongside a failure so heal-path re-reads see it

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

        if error_details is not None:
            param_count += 1
            updates.append(f"error_details = ${param_count}::jsonb")
            values.append(json.dumps(error_details))

        if not updates:
            return False

        updates.append("updated_at = CURRENT_TIMESTAMP")
        param_count += 1
        values.append(uuid_val)

        query = f"UPDATE jobs SET {', '.join(updates)} WHERE id = ${param_count}"

        async with self.acquire() as conn:
            result = await conn.execute(query, *values)

        updated = result == "UPDATE 1"
        if updated and status in ("completed", "failed", "cancelled"):
            # Terminal → the LangGraph checkpoint is dead; prune it. Self-gates on
            # CHECKPOINTER_BACKEND and is non-fatal — cleanup must never break a
            # status update.
            try:
                await self.delete_checkpoint_thread(job_id)
            except Exception as e:
                logger.debug("checkpoint prune skipped for %s: %s", job_id, e)
        return updated

    async def delete_checkpoint_thread(self, thread_id: str) -> int:
        """Prune LangGraph checkpoint rows for a terminal job (thread_id=job_id).

        No-op unless ``CHECKPOINTER_BACKEND=postgres`` (the only mode that writes
        checkpoints to this DB). Mirrors ``AsyncPostgresSaver.adelete_thread``
        across the 3 checkpoint tables. Non-fatal and table-existence tolerant so
        it is safe on a sqlite-backed deployment.

        Returns the number of rows deleted.
        """
        if os.getenv("CHECKPOINTER_BACKEND", "sqlite").strip().lower() != "postgres":
            return 0
        total = 0
        async with self.acquire() as conn:
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                try:
                    result = await conn.execute(
                        f"DELETE FROM {table} WHERE thread_id = $1", thread_id
                    )
                    if isinstance(result, str) and result.startswith("DELETE "):
                        total += int(result.split()[1])
                except Exception as e:
                    logger.debug(
                        "delete_checkpoint_thread: %s skip for %s (%s)",
                        table,
                        thread_id,
                        e,
                    )
        return total

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

    async def update_job_cloud_diff(
        self,
        job_id: str,
        *,
        baseline_commit: str | None = None,
        diff_status: str | None = None,
        clear_diff_status: bool = False,
    ) -> bool:
        """Update the Mode A cloud-diff fields on a job.

        Args:
            job_id: Job UUID as string
            baseline_commit: Gitea commit hash captured at job-start; set once
                at dispatch, never overwritten.
            diff_status: One of 'pending', 'accepted', 'rejected'. NULL is
                only reachable via ``clear_diff_status=True`` (used by reject
                paths that want to allow re-runs).
            clear_diff_status: Explicit opt-in to set ``diff_status=NULL``.
                Required because the default-None argument can't disambiguate
                "leave alone" from "clear".

        Returns:
            True if updated, False if not found / nothing to update.
        """
        try:
            uuid_val = UUID(job_id)
        except ValueError:
            return False

        updates = []
        values = []
        param_count = 0

        if baseline_commit is not None:
            param_count += 1
            updates.append(f"cloud_diff_baseline_commit = ${param_count}")
            values.append(baseline_commit)

        if clear_diff_status:
            updates.append("diff_status = NULL")
        elif diff_status is not None:
            param_count += 1
            updates.append(f"diff_status = ${param_count}")
            values.append(diff_status)

        if not updates:
            return False

        updates.append("updated_at = CURRENT_TIMESTAMP")
        param_count += 1
        values.append(uuid_val)

        query = f"UPDATE jobs SET {', '.join(updates)} WHERE id = ${param_count}"

        async with self.acquire() as conn:
            result = await conn.execute(query, *values)

        return result == "UPDATE 1"

    # ---- protected cloud mode: per-mount RO reader grants (design §8.1.4) ----

    async def create_ro_mount(
        self,
        *,
        thread_id: str,
        user_id: str,
        backend: str,
        reader_id: str,
        grant_handle: str,
        credentials: str | None,
        webdav_url: str,
        auth_kind: str,
    ) -> str:
        """Upsert the per-mount RO grant row (one live grant per thread).

        Encrypts ``credentials`` at rest. A re-provision on the same thread
        replaces the prior grant row in place (ON CONFLICT).
        """
        async with self.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO cloud_ro_mounts
                    (thread_id, user_id, backend, reader_id, grant_handle,
                     credentials, webdav_url, auth_kind, status)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'active')
                ON CONFLICT (thread_id) DO UPDATE SET
                    user_id=$2, backend=$3, reader_id=$4, grant_handle=$5,
                    credentials=$6, webdav_url=$7, auth_kind=$8,
                    status='active', created_at=now(), revoked_at=NULL
                RETURNING id::text
                """,
                thread_id,
                user_id,
                backend,
                reader_id,
                grant_handle,
                _encrypt_optional(credentials),
                webdav_url,
                auth_kind,
            )

    async def get_ro_mount_by_thread(self, thread_id: str) -> dict | None:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM cloud_ro_mounts WHERE thread_id=$1", thread_id
            )
        return self._ro_mount_row(row) if row else None

    async def list_active_ro_mounts(self) -> list[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM cloud_ro_mounts WHERE status='active'"
            )
        return [self._ro_mount_row(r) for r in rows]

    async def list_ro_mounts_for_user(self, user_id: str) -> list[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM cloud_ro_mounts WHERE user_id=$1", user_id
            )
        return [self._ro_mount_row(r) for r in rows]

    async def mark_ro_mount_revoked(self, row_id: str) -> bool:
        async with self.acquire() as conn:
            result = await conn.execute(
                "UPDATE cloud_ro_mounts SET status='revoked', revoked_at=now() "
                "WHERE id=$1 AND status='active'",
                row_id,
            )
        return result == "UPDATE 1"

    async def update_ro_mount_baseline(
        self, row_id: str, baseline: dict[str, str], *, require_active: bool = True
    ) -> bool:
        """Persist the etag baseline (engage-time capture / post-apply re-capture).

        ``require_active=True`` (default, used by engage) scopes the UPDATE to
        ``status = 'active'`` — engage must never resurrect a row the
        reconciler already revoked. Apply/reject's post-write bookkeeping
        passes ``require_active=False``: the reconciler revokes an
        idle/ended thread's row within ~15min regardless of a pending
        review, reads already tolerate a revoked row (Task 8), and a
        revoked-but-otherwise-live staging must still be clearable so a
        late apply/reject on an ended thread doesn't wedge behind a status
        clause that reads have no such gate for.
        """
        query = "UPDATE cloud_ro_mounts SET etag_baseline = $2::jsonb WHERE id = $1"
        if require_active:
            query += " AND status = 'active'"
        async with self.acquire() as conn:
            result = await conn.execute(query, row_id, json.dumps(baseline))
        return result == "UPDATE 1"

    async def update_ro_mount_staging(
        self,
        row_id: str,
        *,
        staged_epoch: int,
        staged_summary: dict | None,
        require_active: bool = True,
    ) -> bool:
        """Advance the staging epoch. staged_summary=None clears the staged state.

        See ``update_ro_mount_baseline`` for the ``require_active`` contract —
        same rationale, same default.
        """
        query = (
            "UPDATE cloud_ro_mounts SET staged_epoch = $2, "
            "staged_summary = $3::jsonb, "
            "staged_at = CASE WHEN $3::text IS NULL THEN NULL ELSE now() END "
            "WHERE id = $1"
        )
        if require_active:
            query += " AND status = 'active'"
        async with self.acquire() as conn:
            result = await conn.execute(
                query,
                row_id,
                staged_epoch,
                json.dumps(staged_summary) if staged_summary is not None else None,
            )
        return result == "UPDATE 1"

    @staticmethod
    def _ro_mount_row(row) -> dict:
        d = dict(row)
        d["credentials"] = _decrypt_stored(
            d.get("credentials"), field="cloud_ro_mounts.credentials"
        )
        for _jf in ("etag_baseline", "staged_summary"):
            if isinstance(d.get(_jf), str):
                d[_jf] = json.loads(d[_jf])
        return d

    async def update_job_exported_folder(
        self,
        job_id: str,
        *,
        handle: str | None,
    ) -> bool:
        """Set or clear the Mode B shared-folder export on a job.

        Passing a non-None ``handle`` stamps both ``exported_folder_handle``
        and ``exported_at = NOW()``. Passing ``None`` clears both (used if
        we ever want to allow re-export after the user has deleted the
        cloud folder; v1 just refuses re-export).

        Args:
            job_id: Job UUID as string
            handle: Opaque cloud handle, or None to clear.

        Returns:
            True if updated, False if not found.
        """
        try:
            uuid_val = UUID(job_id)
        except ValueError:
            return False

        async with self.acquire() as conn:
            if handle is None:
                result = await conn.execute(
                    """
                    UPDATE jobs
                       SET exported_folder_handle = NULL,
                           exported_at = NULL,
                           updated_at = CURRENT_TIMESTAMP
                     WHERE id = $1
                    """,
                    uuid_val,
                )
            else:
                result = await conn.execute(
                    """
                    UPDATE jobs
                       SET exported_folder_handle = $1,
                           exported_at = CURRENT_TIMESTAMP,
                           updated_at = CURRENT_TIMESTAMP
                     WHERE id = $2
                    """,
                    handle,
                    uuid_val,
                )

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

    async def store_resolved_config(
        self, job_id: str, resolved_config: Dict[str, Any]
    ) -> bool:
        """Freeze the resolved config blob into ``jobs.resolved_config``.

        First-run only: writes solely when the column is currently NULL, so a
        re-dispatch / resume never overwrites the original frozen config (the
        config-drift guard, mirroring the agent-side freeze). In the
        orchestrator-resolved architecture the orchestrator owns this freeze.
        The blob MUST already be stripped of secrets (``redact_config_override``)
        by the caller.

        Returns True if the freeze was written, False if not found or already set.
        """
        import json as json_module

        try:
            uuid_val = UUID(str(job_id))
        except ValueError:
            return False

        query = (
            "UPDATE jobs SET resolved_config = $1, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = $2 AND resolved_config IS NULL"
        )
        async with self.acquire() as conn:
            result = await conn.execute(
                query, json_module.dumps(resolved_config), uuid_val
            )

        return result == "UPDATE 1"

    async def merge_job_context(self, job_id: str, updates: Dict[str, Any]) -> bool:
        """Atomically merge updates into the job's context JSONB column.

        Uses PostgreSQL's || operator for a top-level merge — a single atomic
        statement, immune to the read-modify-write lost-update race (a
        concurrent merge into *other* keys is never clobbered). ``updates`` must
        be a JSON object; ``|| $1`` only object-merges when both operands are
        objects.

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

    async def delete_job_context_keys(self, job_id: str, keys: List[str]) -> bool:
        """Atomically remove one or more top-level keys from ``jobs.context``.

        Single-statement ``context - $1::text[]`` so a concurrent merge into
        other keys is preserved (unlike a read-modify-write full-dict rewrite).
        Removing an absent key is a no-op. Used to drain consumed keys such as
        ``queued_feedback``/``delegation_results`` after an agent accepts a
        resume.

        Args:
            job_id: Job UUID as string
            keys: Top-level context keys to delete

        Returns:
            True if the row was updated, False if not found / id invalid / no keys
        """
        if not keys:
            return False

        try:
            uuid_val = UUID(job_id)
        except ValueError:
            return False

        query = (
            "UPDATE jobs "
            "SET context = COALESCE(context, '{}'::jsonb) - $1::text[], "
            "    updated_at = CURRENT_TIMESTAMP "
            "WHERE id = $2"
        )
        async with self.acquire() as conn:
            result = await conn.execute(query, list(keys), uuid_val)

        return result == "UPDATE 1"

    async def append_queued_reply(self, job_id: str, reply: Dict[str, Any]) -> bool:
        """Atomically append one reply object to ``context.queued_replies``.

        Single-statement ``jsonb_set(..., arr || $1)`` so two concurrent inbound
        replies both land (the old read-modify-write full-dict rewrite lost one
        of a racing pair — its RMW window spanned an LLM triage call). Creates
        the array when the key is absent; the ``COALESCE`` guards a NULL column.
        Appending an object to an array yields the object as one element.
        Validated on PG 15.17 (NULL column + missing key). See HF-3 in
        docs/features/database_roadmap.md.

        Args:
            job_id: Job UUID as string
            reply: Reply object to append (thread_id/message/timestamp)

        Returns:
            True if updated, False if not found / id invalid
        """
        import json as json_module

        try:
            uuid_val = UUID(job_id)
        except ValueError:
            return False

        query = (
            "UPDATE jobs "
            "SET context = jsonb_set("
            "        COALESCE(context, '{}'::jsonb), "
            "        '{queued_replies}', "
            "        COALESCE(context->'queued_replies', '[]'::jsonb) || $1::jsonb"
            "    ), "
            "    updated_at = CURRENT_TIMESTAMP "
            "WHERE id = $2"
        )
        async with self.acquire() as conn:
            result = await conn.execute(query, json_module.dumps(reply), uuid_val)

        return result == "UPDATE 1"

    async def increment_job_memory_retry(self, job_id: str) -> int:
        """Atomically increment context.memory_retry_count and return the new value.

        Done as a single DB statement (read-and-increment in one) so concurrent
        /complete handlers — e.g. a duplicate/racing re-dispatch of the same
        paused job — can't both read the same value and stall the counter at 1.
        Returns the new count, or 0 if the job was not found / id was invalid.
        See docs/done/embedding_key_missing_silently_disables_memory_and_kb.md.
        """
        try:
            uuid_val = UUID(job_id)
        except ValueError:
            return 0

        query = (
            "UPDATE jobs "
            "SET context = jsonb_set("
            "        COALESCE(context, '{}'::jsonb), '{memory_retry_count}', "
            "        to_jsonb(COALESCE((context->>'memory_retry_count')::int, 0) + 1)"
            "    ), "
            "    updated_at = CURRENT_TIMESTAMP "
            "WHERE id = $1 "
            "RETURNING (context->>'memory_retry_count')::int"
        )
        async with self.acquire() as conn:
            new_count = await conn.fetchval(query, uuid_val)
        return int(new_count) if new_count is not None else 0

    async def increment_job_verification_round(self, job_id: str) -> int:
        """Atomically increment ``context.verification_round`` and return it.

        Single-statement read-and-increment (mirrors
        :meth:`increment_job_memory_retry`) so racing completion handlers for the
        same critic can't both read the same round and stall the counter.
        Returns the new round, or 0 if the job was not found / id was invalid.
        """
        try:
            uuid_val = UUID(job_id)
        except ValueError:
            return 0

        query = (
            "UPDATE jobs "
            "SET context = jsonb_set("
            "        COALESCE(context, '{}'::jsonb), '{verification_round}', "
            "        to_jsonb(COALESCE((context->>'verification_round')::int, 0) + 1)"
            "    ), "
            "    updated_at = CURRENT_TIMESTAMP "
            "WHERE id = $1 "
            "RETURNING (context->>'verification_round')::int"
        )
        async with self.acquire() as conn:
            new_round = await conn.fetchval(query, uuid_val)
        return int(new_round) if new_round is not None else 0

    async def increment_job_llm_outage_attempt(
        self,
        job_id: str,
        *,
        now: datetime,
        reset_window_seconds: int,
        fingerprint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Atomically advance ``context.llm_outage`` for an ``llm_unavailable`` pause.

        Row-locks the job (``SELECT ... FOR UPDATE``) so a duplicate/racing
        ``/complete`` for the same paused job can't both read the old attempt and
        stall the counter — the same hazard :meth:`increment_job_memory_retry`
        guards, but the conditional auto-reset makes a single-statement increment
        awkward, so this serializes instead. Applies the reset (gap since
        ``last_failed_at`` > ``reset_window_seconds`` → counter + duration ceiling
        restart), increments ``attempt``, and stamps ``first``/``last_failed_at``
        (ISO-8601 UTC). Mirrors :func:`services.completion.evaluate_llm_outage`.

        Returns ``{"attempt": int, "first_failed_at": str|None, "reset": bool}``;
        ``attempt=0`` on a missing/invalid job (caller treats as no-op).
        See docs/features/llm_outage_pause_and_backoff_redispatch.md.
        """
        import json as json_module

        try:
            uuid_val = UUID(job_id)
        except ValueError:
            return {"attempt": 0, "first_failed_at": None, "reset": False}

        def _parse(v: Any) -> Optional[datetime]:
            if not isinstance(v, str) or not v:
                return None
            try:
                dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                return None
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

        now_utc = now.astimezone(timezone.utc)
        async with self.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT context FROM jobs WHERE id = $1 FOR UPDATE", uuid_val
                )
                if row is None:
                    return {"attempt": 0, "first_failed_at": None, "reset": False}
                ctx = row["context"]
                if isinstance(ctx, str):
                    try:
                        ctx = json_module.loads(ctx)
                    except (ValueError, TypeError):
                        ctx = {}
                ctx = ctx if isinstance(ctx, dict) else {}
                outage = ctx.get("llm_outage")
                outage = outage if isinstance(outage, dict) else {}

                attempt = int(outage.get("attempt", 0) or 0)
                first_dt = _parse(outage.get("first_failed_at"))
                last_dt = _parse(outage.get("last_failed_at"))
                next_retry_dt = _parse(outage.get("next_retry_at"))
                # Anchor the reset at the END of any scheduled wait we imposed
                # (next_retry_at), not last_failed_at — a long cooldown pause is
                # us deliberately sleeping, not the job running fine. MUST mirror
                # services.completion.evaluate_llm_outage exactly (the two are
                # kept in lockstep). Missing next_retry_at → today's behavior.
                # docs/features/llm_cooldown_pause_and_resume.md §Design decision
                anchor_dt = last_dt
                if next_retry_dt is not None and (
                    anchor_dt is None or next_retry_dt > anchor_dt
                ):
                    anchor_dt = next_retry_dt
                reset = (
                    anchor_dt is not None
                    and (now_utc - anchor_dt).total_seconds() > reset_window_seconds
                )
                if reset:
                    attempt = 0
                    first_dt = None
                if first_dt is None:
                    first_dt = now_utc
                attempt += 1

                new_outage = {
                    "attempt": attempt,
                    "first_failed_at": first_dt.isoformat(),
                    "last_failed_at": now_utc.isoformat(),
                    # Determinism fail-fast (completion.llm_outage_fingerprint):
                    # overwritten every pause, so the comparison in
                    # determine_job_status is strictly consecutive-identical.
                    # None (non-4xx / outage-shaped error) breaks any streak.
                    "fingerprint": fingerprint,
                }
                await conn.execute(
                    """
                    UPDATE jobs
                       SET context = jsonb_set(
                               COALESCE(context, '{}'::jsonb),
                               '{llm_outage}', $2::jsonb
                           ),
                           updated_at = CURRENT_TIMESTAMP
                     WHERE id = $1
                    """,
                    uuid_val,
                    json_module.dumps(new_outage),
                )
        return {
            "attempt": attempt,
            "first_failed_at": new_outage["first_failed_at"],
            "reset": reset,
        }

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

    async def merge_vm_context_if_current(
        self,
        job_id: str,
        expected_registration_id: str,
        vm_updates: Dict[str, Any],
    ) -> bool:
        """Merge updates into context.vm only if the SSH registration still matches.

        Used by VM SSH-readiness probes so a slow probe for an older VM
        incarnation cannot promote a newer registration to ready.
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
            "WHERE id = $2 "
            "  AND context->'vm'->>'ssh_registration_id' = $3"
        )
        async with self.acquire() as conn:
            result = await conn.execute(
                query,
                json_module.dumps(vm_updates),
                uuid_val,
                expected_registration_id,
            )

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

    async def acquire_docker_workspace_lease(
        self,
        *,
        owner_kind: str,
        owner_id: str,
        candidates: List[Dict[str, Any]],
    ) -> Dict[str, Any] | None:
        """Atomically lease one static Docker workspace across jobs and threads.

        ``docker_workspace_leases`` is the durable occupancy authority. The
        owner JSONB object is only a context mirror and is written in the same
        transaction as an inventory claim. This distinction is essential:
        quarantined inventory must survive permanent deletion of its last job
        or thread owner.

        A previously unseen endpoint is unavailable by default. It is seeded
        ``released`` only by either an explicit, fingerprinted bootstrap
        attestation or the same-trust ``trusted_dev`` mode. ``ON CONFLICT DO
        NOTHING`` makes those flags first-discovery-only; configuration can
        never promote an existing durable quarantine.
        """

        try:
            owner_uuid = UUID(owner_id)
        except (TypeError, ValueError):
            return None
        if owner_kind not in {"job", "thread"}:
            raise ValueError("Docker workspace owner kind must be job or thread")

        normalized: list[dict[str, Any]] = []
        by_endpoint: dict[tuple[str, int], dict[str, Any]] = {}
        for candidate in candidates:
            host = str(candidate.get("host") or "").strip()
            try:
                port = int(candidate.get("port", 22))
            except (TypeError, ValueError) as exc:
                raise ValueError("Docker workspace candidate port is invalid") from exc
            if (
                not host
                or len(host) > 255
                or any(ord(char) < 32 or ord(char) == 127 for char in host)
                or not 1 <= port <= 65535
            ):
                raise ValueError("Docker workspace candidate is invalid")

            bootstrap_attested = candidate.get("bootstrap_attested", False)
            trusted_dev_reuse = candidate.get("trusted_dev_reuse", False)
            if not isinstance(bootstrap_attested, bool) or not isinstance(
                trusted_dev_reuse, bool
            ):
                raise ValueError("Docker workspace trust flags must be boolean")
            fingerprint_value = candidate.get("host_key_fingerprint")
            fingerprint = None
            if fingerprint_value is not None:
                fingerprint = str(fingerprint_value).strip()
                if (
                    not fingerprint.startswith("SHA256:")
                    or len(fingerprint) > 128
                    or any(char.isspace() for char in fingerprint)
                ):
                    raise ValueError("Docker workspace host fingerprint is invalid")
            if bootstrap_attested and fingerprint is None:
                raise ValueError(
                    "Docker workspace bootstrap attestation requires a fingerprint"
                )

            item: dict[str, Any] = {
                "host": host,
                "port": port,
                "host_key_fingerprint": fingerprint,
                "bootstrap_attested": bootstrap_attested,
                "trusted_dev_reuse": trusted_dev_reuse,
            }
            if candidate.get("ide_host"):
                ide_host = str(candidate["ide_host"]).strip()
                if not ide_host or len(ide_host) > 255:
                    raise ValueError("Docker workspace IDE host is invalid")
                item["ide_host"] = ide_host
            if candidate.get("ide_port") is not None:
                try:
                    ide_port = int(candidate["ide_port"])
                except (TypeError, ValueError) as exc:
                    raise ValueError("Docker workspace IDE port is invalid") from exc
                if not 1 <= ide_port <= 65535:
                    raise ValueError("Docker workspace IDE port is invalid")
                item["ide_port"] = ide_port

            endpoint = (host, port)
            prior = by_endpoint.get(endpoint)
            if prior is not None:
                security_fields = (
                    "host_key_fingerprint",
                    "bootstrap_attested",
                    "trusted_dev_reuse",
                )
                if any(
                    prior.get(field) != item.get(field) for field in security_fields
                ):
                    raise ValueError(
                        "Duplicate Docker workspace candidates disagree on trust"
                    )
                continue
            by_endpoint[endpoint] = item
            normalized.append(item)
        if not normalized:
            return None

        select_owner = (
            "SELECT context->'workspace_container' AS workspace "
            "FROM jobs WHERE id = $1 FOR UPDATE"
            if owner_kind == "job"
            else "SELECT metadata->'workspace_container' AS workspace "
            "FROM threads WHERE id = $1 FOR UPDATE"
        )
        update_owner = (
            "UPDATE jobs "
            "SET context = jsonb_set(COALESCE(context, '{}'::jsonb), "
            "'{workspace_container}', $2::jsonb), "
            "updated_at = CURRENT_TIMESTAMP WHERE id = $1"
            if owner_kind == "job"
            else "UPDATE threads "
            "SET metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb), "
            "'{workspace_container}', $2::jsonb), "
            "last_activity = CURRENT_TIMESTAMP WHERE id = $1"
        )

        def parse_workspace(raw: Any) -> dict[str, Any]:
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except (TypeError, ValueError):
                    return {}
            return dict(raw) if isinstance(raw, dict) else {}

        def inventory_dict(row: Any) -> dict[str, Any] | None:
            return dict(row) if row is not None else None

        def mirror_inventory(
            current: dict[str, Any],
            inventory: dict[str, Any],
            *,
            preserve_generation: bool,
        ) -> dict[str, Any]:
            lease_id = inventory.get("lease_id")
            trust_mode = str(inventory.get("trust_mode") or "unattested")
            mirrored = {
                **{
                    key: current[key]
                    for key in _DOCKER_WORKSPACE_PRESERVED_FIELDS
                    if key in current
                },
                "host": str(inventory["host"]),
                "port": int(inventory["port"]),
                "status": str(inventory["status"]),
                "provisioner": "docker",
                _DOCKER_WORKSPACE_LEASE_KEY: str(lease_id) if lease_id else None,
                _DOCKER_WORKSPACE_TRUST_KEY: trust_mode,
                _DOCKER_WORKSPACE_ATTESTED_KEY: trust_mode == "attested",
                "quarantine_reason": inventory.get("quarantine_reason"),
            }
            candidate = by_endpoint.get(
                (str(inventory["host"]), int(inventory["port"]))
            )
            if candidate:
                for field in ("ide_host", "ide_port"):
                    if field in candidate:
                        mirrored[field] = candidate[field]
            if owner_kind == "thread":
                mirrored[_DOCKER_WORKSPACE_GENERATION_KEY] = (
                    current.get(_DOCKER_WORKSPACE_GENERATION_KEY)
                    if preserve_generation
                    else None
                )
            return mirrored

        async with self.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    "srw-docker-workspace-pool",
                )
                owner_row = await conn.fetchrow(select_owner, owner_uuid)
                if owner_row is None:
                    return None
                current = parse_workspace(owner_row["workspace"])

                # First discovery is the only point at which configuration may
                # seed availability. Existing inventory always wins unchanged.
                for candidate in normalized:
                    if candidate["bootstrap_attested"]:
                        seed_status = "released"
                        seed_trust = "attested"
                        seed_fingerprint = candidate["host_key_fingerprint"]
                        seed_reason = None
                    elif candidate["trusted_dev_reuse"]:
                        seed_status = "released"
                        seed_trust = "trusted_dev"
                        seed_fingerprint = candidate["host_key_fingerprint"]
                        seed_reason = None
                    else:
                        seed_status = "quarantined"
                        seed_trust = "unattested"
                        seed_fingerprint = None
                        seed_reason = "bootstrap_attestation_required"
                    await conn.execute(
                        """
                        INSERT INTO docker_workspace_leases (
                            host, port, status, trust_mode,
                            host_key_fingerprint, quarantine_reason
                        )
                        VALUES ($1, $2, $3, $4, $5, $6)
                        ON CONFLICT (host, port) DO NOTHING
                        """,
                        candidate["host"],
                        candidate["port"],
                        seed_status,
                        seed_trust,
                        seed_fingerprint,
                        seed_reason,
                    )

                # If inventory already has a live lease for this owner, it is
                # authoritative even when the JSONB mirror is absent or stale.
                inventory = inventory_dict(
                    await conn.fetchrow(
                        f"""
                        SELECT {_DOCKER_INVENTORY_COLUMNS}
                        FROM docker_workspace_leases
                        WHERE owner_kind = $1 AND owner_id = $2
                          AND status IN ('ready', 'releasing')
                        FOR UPDATE
                        """,
                        owner_kind,
                        owner_uuid,
                    )
                )
                if inventory is not None:
                    endpoint = (str(inventory["host"]), int(inventory["port"]))
                    candidate = by_endpoint.get(endpoint)
                    trust_mode = str(inventory.get("trust_mode") or "unattested")
                    fingerprint_matches = bool(
                        candidate
                        and candidate.get("host_key_fingerprint")
                        == inventory.get("host_key_fingerprint")
                    )
                    try:
                        current_port = int(current.get("port", 22))
                    except (TypeError, ValueError):
                        current_port = -1
                    mirror_same_lease = (
                        current.get("provisioner") == "docker"
                        and str(current.get("host") or "") == endpoint[0]
                        and current_port == endpoint[1]
                        and current.get("status") == inventory.get("status")
                        and str(current.get(_DOCKER_WORKSPACE_LEASE_KEY) or "")
                        == str(inventory.get("lease_id") or "")
                    )
                    current_claims_docker_lease = current.get(
                        "provisioner"
                    ) == "docker" and current.get("status") in {
                        "ready",
                        "releasing",
                        "released",
                        "quarantined",
                    }
                    if current_claims_docker_lease and not mirror_same_lease:
                        # A missing mirror can be repaired from inventory, but
                        # two different live claims must never be reconciled by
                        # silently overwriting one side. Keep the durable host
                        # unavailable and withdraw all capability-bearing
                        # identity from the owner mirror.
                        await conn.execute(
                            """
                            UPDATE docker_workspace_leases
                            SET status = 'quarantined',
                                quarantine_reason = 'owner_mirror_mismatch',
                                updated_at = CURRENT_TIMESTAMP
                            WHERE host = $1 AND port = $2
                              AND lease_id IS NOT DISTINCT FROM $3
                            """,
                            *endpoint,
                            inventory.get("lease_id"),
                        )
                        quarantined = {
                            **{
                                key: current[key]
                                for key in _DOCKER_WORKSPACE_PRESERVED_FIELDS
                                if key in current
                            },
                            "host": endpoint[0],
                            "port": endpoint[1],
                            "status": "quarantined",
                            "provisioner": "docker",
                            _DOCKER_WORKSPACE_LEASE_KEY: None,
                            _DOCKER_WORKSPACE_TRUST_KEY: "unattested",
                            _DOCKER_WORKSPACE_ATTESTED_KEY: False,
                            "quarantine_reason": "owner_mirror_mismatch",
                        }
                        if owner_kind == "thread":
                            quarantined[_DOCKER_WORKSPACE_GENERATION_KEY] = None
                        result = await conn.execute(
                            update_owner, owner_uuid, json.dumps(quarantined)
                        )
                        if result != "UPDATE 1":
                            raise RuntimeError(
                                "Docker workspace owner mirror disappeared during quarantine"
                            )
                        return quarantined
                    if trust_mode == "attested" and not fingerprint_matches:
                        await conn.execute(
                            """
                            UPDATE docker_workspace_leases
                            SET status = 'quarantined',
                                quarantine_reason = 'host_fingerprint_inventory_mismatch',
                                updated_at = CURRENT_TIMESTAMP
                            WHERE host = $1 AND port = $2
                            """,
                            *endpoint,
                        )
                        inventory["status"] = "quarantined"
                        inventory["quarantine_reason"] = (
                            "host_fingerprint_inventory_mismatch"
                        )
                        mirror_same_lease = False
                    elif trust_mode == "trusted_dev" and (
                        not candidate or not candidate["trusted_dev_reuse"]
                    ):
                        await conn.execute(
                            """
                            UPDATE docker_workspace_leases
                            SET status = 'quarantined',
                                quarantine_reason = 'trusted_dev_inventory_not_enabled',
                                updated_at = CURRENT_TIMESTAMP
                            WHERE host = $1 AND port = $2
                            """,
                            *endpoint,
                        )
                        inventory["status"] = "quarantined"
                        inventory["quarantine_reason"] = (
                            "trusted_dev_inventory_not_enabled"
                        )
                        mirror_same_lease = False
                    elif trust_mode not in {"attested", "trusted_dev"}:
                        await conn.execute(
                            """
                            UPDATE docker_workspace_leases
                            SET status = 'quarantined',
                                quarantine_reason = 'inventory_trust_invalid',
                                updated_at = CURRENT_TIMESTAMP
                            WHERE host = $1 AND port = $2
                            """,
                            *endpoint,
                        )
                        inventory["status"] = "quarantined"
                        inventory["quarantine_reason"] = "inventory_trust_invalid"
                        mirror_same_lease = False

                    authoritative = mirror_inventory(
                        current,
                        inventory,
                        preserve_generation=(
                            mirror_same_lease
                            and inventory["status"] == "ready"
                            and trust_mode == "attested"
                        ),
                    )
                    result = await conn.execute(
                        update_owner, owner_uuid, json.dumps(authoritative)
                    )
                    if result != "UPDATE 1":
                        raise RuntimeError(
                            "Docker workspace owner mirror disappeared during lease claim"
                        )
                    return authoritative

                # A non-released mirror without a matching live inventory row
                # is not authority. Mirror a durable quarantine for its endpoint
                # and do not silently hand the owner a different host.
                if (
                    current.get("provisioner") == "docker"
                    and current.get("status") != "released"
                ):
                    try:
                        current_endpoint = (
                            str(current.get("host") or "").strip(),
                            int(current.get("port", 22)),
                        )
                    except (TypeError, ValueError):
                        return None
                    if current_endpoint[0] and 1 <= current_endpoint[1] <= 65535:
                        existing = inventory_dict(
                            await conn.fetchrow(
                                f"""
                                SELECT {_DOCKER_INVENTORY_COLUMNS}
                                FROM docker_workspace_leases
                                WHERE host = $1 AND port = $2
                                FOR UPDATE
                                """,
                                *current_endpoint,
                            )
                        )
                        if existing is None:
                            await conn.execute(
                                """
                                INSERT INTO docker_workspace_leases (
                                    host, port, status, trust_mode,
                                    quarantine_reason
                                )
                                VALUES ($1, $2, 'quarantined', 'unattested',
                                        'owner_inventory_missing')
                                ON CONFLICT (host, port) DO NOTHING
                                """,
                                *current_endpoint,
                            )
                            existing = inventory_dict(
                                await conn.fetchrow(
                                    f"""
                                    SELECT {_DOCKER_INVENTORY_COLUMNS}
                                    FROM docker_workspace_leases
                                    WHERE host = $1 AND port = $2
                                    FOR UPDATE
                                    """,
                                    *current_endpoint,
                                )
                            )
                        if existing is not None and existing["status"] != "released":
                            authoritative = mirror_inventory(
                                current, existing, preserve_generation=False
                            )
                            # The endpoint may be quarantined for a different or
                            # ambiguous legacy owner. Never overwrite that row.
                            if existing.get("owner_id") is not None and (
                                str(existing.get("owner_id")) != str(owner_uuid)
                                or existing.get("owner_kind") != owner_kind
                            ):
                                authoritative = {
                                    **{
                                        key: current[key]
                                        for key in _DOCKER_WORKSPACE_PRESERVED_FIELDS
                                        if key in current
                                    },
                                    "host": current_endpoint[0],
                                    "port": current_endpoint[1],
                                    "status": "quarantined",
                                    "provisioner": "docker",
                                    _DOCKER_WORKSPACE_LEASE_KEY: None,
                                    _DOCKER_WORKSPACE_TRUST_KEY: "unattested",
                                    _DOCKER_WORKSPACE_ATTESTED_KEY: False,
                                    "quarantine_reason": (
                                        "inventory_owned_by_another_lease"
                                    ),
                                }
                                if owner_kind == "thread":
                                    authoritative[_DOCKER_WORKSPACE_GENERATION_KEY] = (
                                        None
                                    )
                            result = await conn.execute(
                                update_owner, owner_uuid, json.dumps(authoritative)
                            )
                            if result != "UPDATE 1":
                                raise RuntimeError(
                                    "Docker workspace owner mirror disappeared during quarantine"
                                )
                            return authoritative

                selected: dict[str, Any] | None = None
                selected_inventory: dict[str, Any] | None = None
                for candidate in normalized:
                    row = inventory_dict(
                        await conn.fetchrow(
                            f"""
                            SELECT {_DOCKER_INVENTORY_COLUMNS}
                            FROM docker_workspace_leases
                            WHERE host = $1 AND port = $2
                            FOR UPDATE
                            """,
                            candidate["host"],
                            candidate["port"],
                        )
                    )
                    if row is None or row["status"] != "released":
                        continue
                    trust_mode = str(row.get("trust_mode") or "unattested")
                    if trust_mode == "attested":
                        if not candidate.get("host_key_fingerprint") or candidate[
                            "host_key_fingerprint"
                        ] != row.get("host_key_fingerprint"):
                            await conn.execute(
                                """
                                UPDATE docker_workspace_leases
                                SET status = 'quarantined',
                                    quarantine_reason =
                                        'host_fingerprint_inventory_mismatch',
                                    updated_at = CURRENT_TIMESTAMP
                                WHERE host = $1 AND port = $2
                                """,
                                candidate["host"],
                                candidate["port"],
                            )
                            continue
                    elif trust_mode == "trusted_dev":
                        if not candidate["trusted_dev_reuse"]:
                            continue
                    else:
                        await conn.execute(
                            """
                            UPDATE docker_workspace_leases
                            SET status = 'quarantined',
                                quarantine_reason = 'inventory_trust_invalid',
                                updated_at = CURRENT_TIMESTAMP
                            WHERE host = $1 AND port = $2
                            """,
                            candidate["host"],
                            candidate["port"],
                        )
                        continue
                    selected = candidate
                    selected_inventory = row
                    break

                if selected is None or selected_inventory is None:
                    return None

                lease_id = uuid4()
                result = await conn.execute(
                    """
                    UPDATE docker_workspace_leases
                    SET status = 'ready',
                        lease_id = $3,
                        owner_kind = $4,
                        owner_id = $5,
                        quarantine_reason = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE host = $1 AND port = $2 AND status = 'released'
                    """,
                    selected["host"],
                    selected["port"],
                    lease_id,
                    owner_kind,
                    owner_uuid,
                )
                if result != "UPDATE 1":
                    raise RuntimeError("Docker workspace inventory claim was lost")

                preserved = {
                    key: current[key]
                    for key in _DOCKER_WORKSPACE_PRESERVED_FIELDS
                    if key in current
                }
                trust_mode = str(selected_inventory["trust_mode"])
                lease = {
                    **preserved,
                    **{
                        key: selected[key]
                        for key in ("host", "port", "ide_host", "ide_port")
                        if key in selected
                    },
                    "status": "ready",
                    "provisioner": "docker",
                    _DOCKER_WORKSPACE_LEASE_KEY: str(lease_id),
                    _DOCKER_WORKSPACE_TRUST_KEY: trust_mode,
                    _DOCKER_WORKSPACE_ATTESTED_KEY: trust_mode == "attested",
                    "quarantine_reason": None,
                }
                if owner_kind == "thread":
                    # Pairing is a second exact-lease CAS. Until that succeeds,
                    # Canvas remains unavailable even for attested inventory.
                    lease[_DOCKER_WORKSPACE_GENERATION_KEY] = None
                owner_result = await conn.execute(
                    update_owner, owner_uuid, json.dumps(lease)
                )
                if owner_result != "UPDATE 1":
                    # Raising (rather than returning None) rolls the inventory
                    # claim back with the owner-mirror write.
                    raise RuntimeError(
                        "Docker workspace owner mirror disappeared during lease claim"
                    )
                return lease

    async def transition_docker_workspace_lease(
        self,
        *,
        owner_kind: str,
        owner_id: str,
        expected_lease_id: str | None,
        expected_statuses: set[str],
        updates: Dict[str, Any],
    ) -> Dict[str, Any] | None:
        """Compare-and-merge one exact durable Docker inventory lease.

        The inventory endpoint, owner, lease ID, and status must all agree.
        This serializes Canvas pairing, release, finalization, and allocation
        across replicas. A stale owner mirror can never mutate a newly assigned
        inventory row. Owner and inventory updates commit together; the narrow
        owner-missing finalization case keeps an already-claimed endpoint
        occupied or safely finalizes an exact post-cleanup lease.
        """

        try:
            owner_uuid = UUID(owner_id)
        except (TypeError, ValueError):
            return None
        if owner_kind not in {"job", "thread"}:
            raise ValueError("Docker workspace owner kind must be job or thread")
        valid_statuses = {"ready", "releasing", "released", "quarantined"}
        if not expected_statuses or not expected_statuses <= valid_statuses:
            raise ValueError("Docker workspace expected status is invalid")
        status = updates.get("status")
        if status not in valid_statuses:
            raise ValueError("Docker workspace target status is invalid")
        allowed_updates = {
            "status",
            "quarantine_reason",
            _DOCKER_WORKSPACE_GENERATION_KEY,
            _DOCKER_WORKSPACE_TRUST_KEY,
            _DOCKER_WORKSPACE_ATTESTED_KEY,
        }
        if not set(updates) <= allowed_updates:
            raise ValueError("Docker workspace transition contains unsupported fields")

        expected_uuid: UUID | None = None
        if expected_lease_id is not None:
            try:
                expected_uuid = UUID(str(expected_lease_id))
            except (TypeError, ValueError):
                return None

        requested_trust = updates.get(_DOCKER_WORKSPACE_TRUST_KEY)
        requested_attested = updates.get(_DOCKER_WORKSPACE_ATTESTED_KEY)
        if requested_trust is not None and requested_trust not in {
            "unattested",
            "trusted_dev",
            "attested",
        }:
            raise ValueError("Docker workspace target trust mode is invalid")
        if requested_attested is not None and not isinstance(requested_attested, bool):
            raise ValueError("Docker workspace attested marker must be boolean")
        if (
            requested_trust is not None
            and requested_attested is not None
            and requested_attested != (requested_trust == "attested")
        ):
            raise ValueError("Docker workspace trust markers disagree")

        select_owner = (
            "SELECT context->'workspace_container' AS workspace "
            "FROM jobs WHERE id = $1 FOR UPDATE"
            if owner_kind == "job"
            else "SELECT metadata->'workspace_container' AS workspace "
            "FROM threads WHERE id = $1 FOR UPDATE"
        )
        update_owner = (
            "UPDATE jobs "
            "SET context = jsonb_set(COALESCE(context, '{}'::jsonb), "
            "'{workspace_container}', $2::jsonb), "
            "updated_at = CURRENT_TIMESTAMP WHERE id = $1"
            if owner_kind == "job"
            else "UPDATE threads "
            "SET metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb), "
            "'{workspace_container}', $2::jsonb), "
            "last_activity = CURRENT_TIMESTAMP WHERE id = $1"
        )

        async with self.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    "srw-docker-workspace-pool",
                )
                owner_row = await conn.fetchrow(select_owner, owner_uuid)
                current: dict[str, Any] | None = None
                if owner_row is not None:
                    raw_current = owner_row["workspace"] or {}
                    if isinstance(raw_current, str):
                        try:
                            raw_current = json.loads(raw_current)
                        except (TypeError, ValueError):
                            raw_current = {}
                    if isinstance(raw_current, dict):
                        current = dict(raw_current)

                inventory = None
                if expected_uuid is not None:
                    inventory_row = await conn.fetchrow(
                        f"""
                        SELECT {_DOCKER_INVENTORY_COLUMNS}
                        FROM docker_workspace_leases
                        WHERE owner_kind = $1 AND owner_id = $2 AND lease_id = $3
                        FOR UPDATE
                        """,
                        owner_kind,
                        owner_uuid,
                        expected_uuid,
                    )
                    inventory = dict(inventory_row) if inventory_row else None
                elif (
                    current is not None
                    and current.get(_DOCKER_WORKSPACE_LEASE_KEY) is None
                ):
                    # Legacy NULL is a real CAS value, never a wildcard. Durable
                    # migration inventory is quarantined, so this path can only
                    # match an explicitly owner-associated NULL lease.
                    try:
                        current_host = str(current.get("host") or "").strip()
                        current_port = int(current.get("port", 22))
                    except (TypeError, ValueError):
                        return None
                    inventory_row = await conn.fetchrow(
                        f"""
                        SELECT {_DOCKER_INVENTORY_COLUMNS}
                        FROM docker_workspace_leases
                        WHERE host = $1 AND port = $2
                          AND owner_kind = $3 AND owner_id = $4
                          AND lease_id IS NULL
                        FOR UPDATE
                        """,
                        current_host,
                        current_port,
                        owner_kind,
                        owner_uuid,
                    )
                    inventory = dict(inventory_row) if inventory_row else None

                if (
                    inventory is None
                    or inventory.get("status") not in expected_statuses
                ):
                    return None

                allowed_targets = {
                    "ready": {"ready", "releasing", "quarantined"},
                    "releasing": {"released", "quarantined"},
                    "released": set(),
                    "quarantined": {"quarantined"},
                }
                inventory_status = str(inventory["status"])
                if status not in allowed_targets[inventory_status]:
                    raise ValueError("Docker workspace lifecycle transition is invalid")

                owner_matches = False
                if current is not None:
                    try:
                        owner_matches = (
                            current.get("provisioner") == "docker"
                            and current.get("status") == inventory_status
                            and str(current.get("host") or "") == str(inventory["host"])
                            and int(current.get("port", 22)) == int(inventory["port"])
                            and (
                                (
                                    expected_lease_id is None
                                    and current.get(_DOCKER_WORKSPACE_LEASE_KEY) is None
                                )
                                or str(current.get(_DOCKER_WORKSPACE_LEASE_KEY) or "")
                                == str(expected_uuid)
                            )
                        )
                    except (TypeError, ValueError):
                        owner_matches = False

                if current is not None and not owner_matches:
                    # Inventory is authoritative, but a live disagreement means
                    # neither side is safe to use. Quarantine the exact lease;
                    # never redirect the stale mirror to another endpoint.
                    await conn.execute(
                        """
                        UPDATE docker_workspace_leases
                        SET status = 'quarantined',
                            quarantine_reason = 'owner_mirror_mismatch',
                            updated_at = CURRENT_TIMESTAMP
                        WHERE host = $1 AND port = $2 AND lease_id IS NOT DISTINCT FROM $3
                        """,
                        inventory["host"],
                        inventory["port"],
                        inventory.get("lease_id"),
                    )
                    quarantined = {
                        **{
                            key: current[key]
                            for key in _DOCKER_WORKSPACE_TRANSITION_PRESERVED_FIELDS
                            if key in current
                        },
                        "host": str(inventory["host"]),
                        "port": int(inventory["port"]),
                        "status": "quarantined",
                        "provisioner": "docker",
                        "quarantine_reason": "owner_mirror_mismatch",
                        _DOCKER_WORKSPACE_LEASE_KEY: None,
                        _DOCKER_WORKSPACE_TRUST_KEY: "unattested",
                        _DOCKER_WORKSPACE_GENERATION_KEY: None,
                        _DOCKER_WORKSPACE_ATTESTED_KEY: False,
                    }
                    result = await conn.execute(
                        update_owner, owner_uuid, json.dumps(quarantined)
                    )
                    if result != "UPDATE 1":
                        raise RuntimeError(
                            "Docker workspace owner mirror disappeared during quarantine"
                        )
                    return None

                if current is None and status not in {"released", "quarantined"}:
                    # Pairing/releasing without an owner is nonsensical. Preserve
                    # the endpoint as an owner-independent quarantine.
                    await conn.execute(
                        """
                        UPDATE docker_workspace_leases
                        SET status = 'quarantined',
                            quarantine_reason = 'owner_deleted_during_transition',
                            updated_at = CURRENT_TIMESTAMP
                        WHERE host = $1 AND port = $2 AND lease_id = $3
                        """,
                        inventory["host"],
                        inventory["port"],
                        inventory["lease_id"],
                    )
                    return None

                current_trust = str(inventory.get("trust_mode") or "unattested")
                target_trust = str(requested_trust or current_trust)
                if status == "released" and requested_trust is None:
                    # Releasing an endpoint is a trust-boundary operation. A
                    # controller must explicitly preserve attested provenance;
                    # dev cleanup must explicitly downgrade to trusted_dev.
                    raise ValueError(
                        "Docker workspace release requires an explicit trust mode"
                    )
                if target_trust == "attested" and current_trust != "attested":
                    raise ValueError(
                        "Docker workspace trust cannot be promoted by a lease transition"
                    )
                if target_trust != current_trust and not (
                    inventory["status"] == "releasing"
                    and status in {"released", "quarantined"}
                    and target_trust == "trusted_dev"
                ):
                    raise ValueError("Docker workspace trust transition is invalid")
                target_fingerprint = inventory.get("host_key_fingerprint")
                if target_trust == "trusted_dev":
                    target_fingerprint = None
                if target_trust == "attested" and not target_fingerprint:
                    raise ValueError("Attested Docker inventory has no fingerprint")

                if status == "quarantined":
                    quarantine_reason = str(
                        updates.get("quarantine_reason") or "lease_quarantined"
                    )
                else:
                    quarantine_reason = None

                inventory_result = await conn.execute(
                    """
                    UPDATE docker_workspace_leases
                    SET status = $4,
                        trust_mode = $5,
                        host_key_fingerprint = $6,
                        quarantine_reason = $7,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE host = $1 AND port = $2
                      AND lease_id IS NOT DISTINCT FROM $3
                    """,
                    inventory["host"],
                    inventory["port"],
                    inventory.get("lease_id"),
                    status,
                    target_trust,
                    target_fingerprint,
                    quarantine_reason,
                )
                if inventory_result != "UPDATE 1":
                    raise RuntimeError("Docker workspace inventory transition was lost")

                replacement_base = {
                    key: current[key]
                    for key in _DOCKER_WORKSPACE_TRANSITION_PRESERVED_FIELDS
                    if current is not None and key in current
                }
                replacement = {
                    **replacement_base,
                    "host": str(inventory["host"]),
                    "port": int(inventory["port"]),
                    "status": status,
                    "provisioner": "docker",
                    _DOCKER_WORKSPACE_LEASE_KEY: (
                        str(inventory["lease_id"])
                        if inventory.get("lease_id") is not None
                        else None
                    ),
                    _DOCKER_WORKSPACE_TRUST_KEY: target_trust,
                    _DOCKER_WORKSPACE_ATTESTED_KEY: target_trust == "attested",
                    "quarantine_reason": quarantine_reason,
                }
                if owner_kind == "thread":
                    if status != "ready":
                        replacement[_DOCKER_WORKSPACE_GENERATION_KEY] = None
                    elif _DOCKER_WORKSPACE_GENERATION_KEY in updates:
                        replacement[_DOCKER_WORKSPACE_GENERATION_KEY] = updates[
                            _DOCKER_WORKSPACE_GENERATION_KEY
                        ]
                    elif current is not None:
                        replacement[_DOCKER_WORKSPACE_GENERATION_KEY] = current.get(
                            _DOCKER_WORKSPACE_GENERATION_KEY
                        )
                if current is not None:
                    owner_result = await conn.execute(
                        update_owner, owner_uuid, json.dumps(replacement)
                    )
                    if owner_result != "UPDATE 1":
                        raise RuntimeError(
                            "Docker workspace owner mirror disappeared during transition"
                        )
                return replacement

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

    async def bind_thread_workspace_backing(
        self,
        thread_id: str,
        *,
        backing_kind: str,
        backing_id: str,
        ssh_host_key_fingerprint: str | None = None,
    ) -> dict[str, Any] | None:
        """Atomically bind a thread to one concrete workspace generation.

        Ordinary status/endpoint merges deliberately do not touch this
        identity. Repeating the same binding is a no-op; a different backing
        identifier, kind, or pinned SSH host key mints a new opaque generation.
        This keeps generation and fingerprint paired under one row lock.
        """

        try:
            uuid_val = UUID(thread_id)
        except ValueError:
            return None
        if backing_kind not in {"remote", "virtual"}:
            raise ValueError("workspace backing kind must be remote or virtual")
        backing_id = str(backing_id).strip()
        if not backing_id or len(backing_id) > 512 or "\x00" in backing_id:
            raise ValueError("workspace backing id is invalid")
        if backing_kind == "remote" and not ssh_host_key_fingerprint:
            raise ValueError("remote workspace binding requires a host fingerprint")
        if ssh_host_key_fingerprint is not None:
            ssh_host_key_fingerprint = ssh_host_key_fingerprint.strip()
            if (
                not ssh_host_key_fingerprint.startswith("SHA256:")
                or len(ssh_host_key_fingerprint) > 128
                or any(ch.isspace() for ch in ssh_host_key_fingerprint)
            ):
                raise ValueError("SSH host-key fingerprint is invalid")

        async with self.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT metadata FROM threads WHERE id = $1 FOR UPDATE",
                    uuid_val,
                )
                if row is None:
                    return None
                metadata = row.get("metadata") or {}
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except (TypeError, ValueError):
                        metadata = {}
                if not isinstance(metadata, dict):
                    metadata = {}

                binding = metadata.get("_workspace_binding") or {}
                if not isinstance(binding, dict):
                    binding = {}
                generation = binding.get("generation")
                valid_generation = False
                try:
                    UUID(str(generation))
                    valid_generation = True
                except (TypeError, ValueError):
                    pass
                unchanged = (
                    valid_generation
                    and binding.get("kind") == backing_kind
                    and binding.get("backing_id") == backing_id
                    and binding.get("ssh_host_key_fingerprint")
                    == ssh_host_key_fingerprint
                )
                if unchanged:
                    return {
                        "workspace_generation": str(generation),
                        "changed": False,
                    }

                generation = str(uuid4())
                patch = {
                    "_workspace_binding": {
                        "generation": generation,
                        "kind": backing_kind,
                        "backing_id": backing_id,
                        "ssh_host_key_fingerprint": ssh_host_key_fingerprint,
                    }
                }
                await conn.execute(
                    """
                    UPDATE threads
                    SET metadata = COALESCE(metadata, '{}'::jsonb) || $2::jsonb,
                        last_activity = CURRENT_TIMESTAMP
                    WHERE id = $1
                    """,
                    uuid_val,
                    json.dumps(patch),
                )
                return {"workspace_generation": generation, "changed": True}

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

    async def merge_thread_vm_context_if_current(
        self,
        thread_id: str,
        expected_registration_id: str,
        vm_updates: Dict[str, Any],
    ) -> bool:
        """Merge updates into metadata.vm only if the SSH registration matches."""
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
            "WHERE id = $2 "
            "  AND metadata->'vm'->>'ssh_registration_id' = $3"
        )
        async with self.acquire() as conn:
            result = await conn.execute(
                query,
                json_module.dumps(vm_updates),
                uuid_val,
                expected_registration_id,
            )

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

        # The read-modify-write below is not atomic; two concurrent merges
        # (e.g. rapid live config.update frames) would silently drop one.
        # Serialize on a per-thread advisory lock, domain-salted so merges
        # never queue behind the (potentially minutes-long) provisioning
        # lock in thread_advisory_lock, and taken on this same connection
        # so no second pool slot is held while waiting.
        h = hashlib.blake2b(
            b"config_override:" + thread_id.encode(), digest_size=8
        ).digest()
        lock_key = int.from_bytes(h, byteorder="big", signed=True)

        async with self.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock($1)", lock_key)

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

    async def set_thread_datasource_ids(
        self, thread_id: str, datasource_ids: List[str]
    ) -> bool:
        """Replace ``threads.metadata.datasource_ids`` with the given full set.

        ``datasource_ids`` is a top-level metadata key (sibling of
        ``config_override`` — ``merge_thread_config_override`` can't touch it),
        and the live-settings protocol sends the desired FULL selection each
        time (idempotent, matching create semantics), so this is a plain
        replace: last write wins is the correct desired-state outcome and no
        read-modify-write lock is needed.

        Args:
            thread_id: Thread UUID as string
            datasource_ids: Complete new selection (may be empty to detach all)

        Returns:
            True if updated, False if thread not found
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
            "    '{datasource_ids}', "
            "    $1::jsonb"
            "), "
            "    last_activity = CURRENT_TIMESTAMP "
            "WHERE id = $2"
        )
        async with self.acquire() as conn:
            result = await conn.execute(
                query,
                json_module.dumps([str(v) for v in datasource_ids]),
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
        pod_uid: str | None = None,
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
                            metadata = COALESCE(metadata, '{}') || $8::jsonb,
                            pod_uid       = COALESCE(NULLIF($9, ''), pod_uid)
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
                        pod_uid,
                    )
                    return {
                        "agent_id": str(agent_id),
                        "heartbeat_interval_seconds": 60,
                    }

            # Create new agent
            row = await conn.fetchrow(
                """
                INSERT INTO agents (config_name, hostname, pod_ip, pod_port, pid, agent_mode, thread_id, metadata, pod_uid)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, NULLIF($9, ''))
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
                pod_uid,
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
        aux_degraded: bool | None = None,
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
            aux_degraded: Auxiliary-model health from the heartbeat
                (metrics.aux.degraded). True/False updates agents.aux_degraded;
                None leaves the persisted flag untouched (e.g. older agent
                builds or a not-yet-wired aux LLM). See aux Phase 2.

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
                "SELECT status, intents, metadata FROM agents WHERE id = $1",
                uuid_val,
            )
            if not prev:
                return None

            prev_status = prev["status"]
            prev_metadata_raw = prev["metadata"] if "metadata" in prev else None
            if isinstance(prev_metadata_raw, str):
                try:
                    prev_metadata = json.loads(prev_metadata_raw)
                except (json.JSONDecodeError, TypeError, ValueError):
                    prev_metadata = {}
            elif isinstance(prev_metadata_raw, dict):
                prev_metadata = prev_metadata_raw
            else:
                prev_metadata = {}
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
            #
            # Offline pin: once the lifecycle reconciler has marked the agent
            # offline (because its pod is gone), a late in-flight heartbeat
            # from the pod's SIGTERM grace period must not be allowed to
            # resurrect the row back to 'ready'. An agent that wants to
            # rejoin must re-/register, which mints a new agent_id.
            # Without this guard, ``_find_idle_persistent_agent`` cheerfully
            # matches the dead row (most-recent ``last_heartbeat`` puts it
            # at the top of the pool query) and ``_send_session_attach``
            # binds the thread to a pod that's about to disappear — see
            # the 2026-05-24 thread 352144ea regression.
            # Build the UPDATE dynamically: status / current_job_id /
            # last_heartbeat always change; the metadata merge,
            # last_completed_at and aux_degraded are conditional. The local
            # positional-arg helper keeps the $N indices correct no matter
            # which optional clauses are present (the previous hand-numbered
            # two-branch form made adding a column an index-shuffling hazard).
            params: List[Any] = []

            def _p(value: Any) -> str:
                params.append(value)
                return f"${len(params)}"

            status_expr = (
                "CASE WHEN status = 'draining' THEN 'draining' "
                "WHEN status = 'offline' THEN 'offline' "
                f"ELSE {_p(status)} END"
            )
            set_clauses = [
                f"status = {status_expr}",
                f"current_job_id = {_p(job_uuid)}",
                "last_heartbeat = CURRENT_TIMESTAMP",
            ]
            if metrics:
                metrics_payload: Dict[str, Any] = dict(metrics)
                graph_progress = metrics_payload.get("graph_progress")
                if graph_progress is not None:
                    previous_progress = prev_metadata.get("graph_progress")
                    if graph_progress != previous_progress:
                        metrics_payload["graph_progress_seen_at"] = datetime.now(
                            timezone.utc
                        ).isoformat()
                set_clauses.append(
                    f"metadata = metadata || {_p(json.dumps(metrics_payload))}::jsonb"
                )
            if set_completed:
                set_clauses.append("last_completed_at = CURRENT_TIMESTAMP")
            if aux_degraded is not None:
                set_clauses.append(f"aux_degraded = {_p(aux_degraded)}")

            query = (
                f"UPDATE agents SET {', '.join(set_clauses)} WHERE id = {_p(uuid_val)}"
            )
            result = await conn.execute(query, *params)

            if result != "UPDATE 1":
                return None

            # Job execution lease renewal (docs/features/job_execution_lease.md):
            # a heartbeat that carries a job renews that job's lease — liveness
            # by renewal, no dependency on agent-row reconciliation. The
            # assigned_agent_id guard fences an agent that lost the job (it
            # cannot extend a lease it no longer owns); the throttle predicate
            # skips the write while >JOB_LEASE_RENEW_BELOW_SECONDS remain so
            # 5s heartbeats don't churn the row.
            if job_uuid is not None and status == "working":
                await conn.execute(
                    """
                    UPDATE jobs
                       SET lease_expires_at = NOW() + make_interval(secs => $3::int)
                     WHERE id = $1
                       AND assigned_agent_id = $2
                       AND status = 'processing'
                       AND (
                           lease_expires_at IS NULL
                           OR lease_expires_at
                              < NOW() + make_interval(secs => $4::int)
                       )
                    """,
                    job_uuid,
                    uuid_val,
                    JOB_LEASE_RUN_SECONDS,
                    JOB_LEASE_RENEW_BELOW_SECONDS,
                )

            if prev_status == "draining":
                effective_status = "draining"
            elif prev_status == "offline":
                effective_status = "offline"
            else:
                effective_status = status
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
                           last_completed_at, metadata, pod_uid, aux_degraded
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
                           last_completed_at, metadata, pod_uid, aux_degraded
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
                       registered_at, last_heartbeat, metadata, pod_uid,
                       aux_degraded
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
        dispatcher can reassign them. Also clears stale assignments on
        'waiting' jobs (status kept) and on already-'paused' jobs (which the
        dispatcher ignores while an agent id is attached).

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

            # And on paused jobs: the dispatcher only resumes paused jobs with
            # assigned_agent_id IS NULL, so a paused job still pointing at an
            # offline agent (e.g. a freeze→paused path that didn't clear the
            # assignment) is un-dispatchable until the 24h agent GC's FK
            # cascade — sweep it free here instead.
            result3 = await conn.execute(
                """
                UPDATE jobs
                SET assigned_agent_id = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE status = 'paused'
                  AND assigned_agent_id IS NOT NULL
                  AND assigned_agent_id IN (
                      SELECT id FROM agents WHERE status = 'offline'
                  )
                """
            )

            # Paused jobs parked for AUTO-redispatch must also shed their
            # row-level freeze blob — get_dispatchable_jobs requires
            # ``freeze_data IS NULL``, so a kept freeze hides the job from the
            # dispatcher forever. Stash it in context.last_freeze_data (state
            # continuity lives in the checkpoint + pushed branch). Covers rows
            # paused by pre-fix code crossing a deploy; /complete now does the
            # same at the source (services/completion.py
            # AUTO_REDISPATCH_FREEZE_TYPES — keep the two lists in sync).
            result4 = await conn.execute(
                """
                UPDATE jobs
                SET freeze_data = NULL,
                    context = COALESCE(context, '{}'::jsonb)
                              || jsonb_build_object('last_freeze_data', freeze_data),
                    updated_at = CURRENT_TIMESTAMP
                WHERE status = 'paused'
                  AND assigned_agent_id IS NULL
                  AND freeze_data->>'freeze_type' IN (
                      'version_upgrade', 'memory_unavailable',
                      'kb_unavailable', 'workspace_upgrade_required'
                  )
                """
            )

        count = 0
        if result.startswith("UPDATE "):
            count += int(result.split()[1])
        if result2.startswith("UPDATE "):
            count += int(result2.split()[1])
        if result3.startswith("UPDATE "):
            count += int(result3.split()[1])
        if result4.startswith("UPDATE "):
            count += int(result4.split()[1])
        return count

    async def recover_expired_lease_jobs(self) -> List[str]:
        """Pause processing jobs whose execution lease has expired.

        The lease is the direct liveness signal (docs/features/
        job_execution_lease.md): ``claim_job_for_agent`` sets a pickup lease,
        the assigned agent's heartbeats renew it, and expiry — checked purely
        against the DATABASE clock — IS the definition of orphaned. Unlike
        :meth:`recover_orphaned_jobs` this needs no join to ``agents``, no
        offline-marking to have run first, and no sweep ordering; it is the
        primary recovery path, with the agents-table sweep kept as
        belt-and-suspenders during the soak (see the feature doc's rollout).

        ``lease_expires_at IS NOT NULL`` keeps pre-lease rows (claimed before
        the deploy, never backfilled) with the legacy sweep instead of
        recovering them instantly.

        Returns:
            The recovered job ids (callers log each — an expired lease is an
            incident signal, not routine noise).
        """
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE jobs
                   SET status = 'paused',
                       assigned_agent_id = NULL,
                       lease_expires_at = NULL,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE status = 'processing'
                   AND lease_expires_at IS NOT NULL
                   AND lease_expires_at < NOW()
                RETURNING id
                """
            )
        return [str(row["id"]) for row in rows]

    async def cancel_stale_verification_subjobs(
        self, stale_hours: int = 6, outage_grace_minutes: int = 30
    ) -> int:
        """Cancel orphaned critic/verification subjobs that can never progress.

        A verification subjob (``context->>'verification_target'`` set) is reaped
        when it is unassigned and either (a) its parent is already terminal, or
        (b) it has sat agentless past ``stale_hours``. These otherwise linger as
        priority-10 dispatchable jobs that parasitically preempt real work (and
        hold the parent's workspace). ``reviewing`` is deliberately not a cancel
        trigger — that is the parent a live critic is reviewing; the staleness
        fallback clears a critic whose parent is *stuck* reviewing without
        killing an active one.

        The staleness arm exempts a critic legitimately paused for an LLM
        outage/cooldown: ``paused`` with a ``context.llm_outage.next_retry_at``
        newer than ``outage_grace_minutes`` ago (the grace covers the outage
        sweeper's claim→dispatch pickup window). A cooldown can exceed the 6h
        horizon while well inside the 12h pause budget, and the paused row's
        ``updated_at`` never refreshes — without the exemption the critic was
        cancelled mid-pause. An OVERDUE wake (paused long past next_retry_at =
        the outage sweeper broke) and the parent-terminal arm still reap.
        docs/features/llm_outage_subjob_resilience.md (#7).

        See docs/done/preemption_before_first_checkpoint_replays_job_opening.md
        and critic_failure_leaves_parent_job_stuck_reviewing.md.

        Args:
            stale_hours: agentless-age fallback horizon for non-terminal parents.
            outage_grace_minutes: how far past its outage wake a paused critic
                stays exempt from the staleness reap.

        Returns:
            Number of subjobs cancelled.
        """
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE jobs AS j
                SET status = 'cancelled',
                    assigned_agent_id = NULL,
                    updated_at = CURRENT_TIMESTAMP
                FROM jobs AS parent
                WHERE j.parent_job_id = parent.id
                  AND j.context->>'verification_target' IS NOT NULL
                  AND j.status IN ('created', 'paused')
                  AND j.assigned_agent_id IS NULL
                  AND (
                        parent.status IN ('completed', 'failed', 'cancelled')
                     OR (
                            j.updated_at
                            < CURRENT_TIMESTAMP - make_interval(hours => $1::int)
                        -- COALESCE guards the NULL case: a paused critic with
                        -- no llm_outage state must stay reapable, and NULL
                        -- would otherwise silently exempt it.
                        AND NOT COALESCE(
                            j.status = 'paused'
                            AND (j.context->'llm_outage'->>'next_retry_at')::timestamptz
                                > CURRENT_TIMESTAMP
                                  - make_interval(mins => $2::int),
                            false
                        )
                     )
                  )
                """,
                stale_hours,
                outage_grace_minutes,
            )

        if result.startswith("UPDATE "):
            return int(result.split()[1])
        return 0

    async def unstick_reviewing_parents(
        self, grace_minutes: int = 30
    ) -> List[Dict[str, Any]]:
        """Un-stick parents wedged in 'reviewing' whose critic pipeline is dead.

        A parent goes 'reviewing' and a critic verification subjob checks its
        work; the critic un-sticks the parent only by reaching its own
        /complete (``_handle_critic_verdict_on_complete``). A critic that ends
        'failed', or is orphaned → cancelled, never reaches that handler, so the
        parent sits in 'reviewing' forever. This flips such a parent back to
        'pending_review' (human review takes over) + lets the caller notify.

        Fires only when EVERY critic child is terminal-failed/cancelled (or none
        exists) and the parent has been reviewing past ``grace_minutes``:

        - A live critic ('processing'/'created'/'paused'/'waiting…') keeps the
          parent untouched — a long review or a recovering (paused) critic is
          never pre-empted.
        - A 'completed' critic also keeps the parent untouched — that is the
          verdict handler's job; excluding it avoids racing that handler.
        - The grace floor on ``updated_at`` dodges the critic-spawn and
          in-flight-verdict windows.

        The CAS (``WHERE status='reviewing'``) makes it idempotent and safe
        against a concurrent sweeper / the verdict handler.

        See docs/superpowers/specs/2026-07-05-reviewing-parent-unstick-watchdog-design.md
        and critic_failure_leaves_parent_job_stuck_reviewing.md (#4).

        Args:
            grace_minutes: minimum time a parent must have been in 'reviewing'.

        Returns:
            One dict ``{id, user_id, config_name}`` per parent un-stuck.
        """
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE jobs AS p
                   SET status = 'pending_review',
                       error_message = 'Automated verification did not complete (critic pipeline died); returned to manual review.',
                       updated_at = CURRENT_TIMESTAMP
                 WHERE p.status = 'reviewing'
                   AND p.updated_at
                       < CURRENT_TIMESTAMP - make_interval(mins => $1::int)
                   AND NOT EXISTS (
                         SELECT 1 FROM jobs c
                          WHERE c.parent_job_id = p.id
                            AND c.context->>'verification_target' IS NOT NULL
                            AND c.status NOT IN ('failed', 'cancelled')
                       )
                RETURNING p.id, p.user_id, p.config_name
                """,
                grace_minutes,
            )
        return [dict(r) for r in rows]

    async def get_ready_agents(self) -> List[Dict[str, Any]]:
        """Get all agents with 'ready' status.

        Returns:
            List of ready agent dicts
        """
        return await self.list_agents(status="ready")

    # =========================================================================
    # DISPATCHER QUERIES (Auto-Assignment)
    # =========================================================================

    async def claim_job_for_agent(self, job_id: str, agent_id: str) -> bool:
        """Atomically claim a dispatchable job for an agent (M1 — HA dispatch).

        Returns True iff THIS call won the claim. The CAS predicate
        (``assigned_agent_id IS NULL AND status IN ('created','paused')``) makes
        the assignment safe against concurrent dispatchers and the transient
        dual-leader window that leader election cannot fence — a job can never
        be handed to two agents. Callers MUST claim before notifying the agent.
        """
        try:
            job_uuid = UUID(job_id)
            agent_uuid = UUID(agent_id)
        except ValueError:
            return False
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE jobs
                   SET status = 'processing',
                       assigned_agent_id = $2,
                       lease_expires_at = NOW() + make_interval(
                           secs => $3::int
                       ),
                       updated_at = CURRENT_TIMESTAMP
                 WHERE id = $1
                   AND assigned_agent_id IS NULL
                   AND status IN ('created', 'paused')
                RETURNING id
                """,
                job_uuid,
                agent_uuid,
                JOB_LEASE_PICKUP_SECONDS,
            )
            return row is not None

    async def claim_delegation_resume(self, job_id: str) -> bool:
        """Atomically transition a delegation parent waiting → paused.

        Returns True iff THIS call performed the transition (clearing the
        agent so the dispatcher re-queues it for resume). Two delegation
        timeout sweepers in the transient dual-leader window both detect the
        same elapsed timeout; this CAS (``WHERE status = 'waiting'``) ensures
        exactly one re-queues the parent, so it is never resumed twice with
        partial results. Mirrors update_job_status' assigned_agent_id → NULL.

        Also clears the delegation ``freeze_data`` blob — ``get_dispatchable_jobs``
        requires ``freeze_data IS NULL`` (partial-index contract, 0046), so a
        kept freeze would hide the re-queued parent from the dispatcher forever
        (docs/issues/delegation_freeze_lifecycle_gaps.md, Gap 1). Nothing reads
        the blob back: children are re-derived from parent_job_id +
        creation_order, and the results ride ``context.delegation_results``.
        """
        try:
            job_uuid = UUID(job_id)
        except ValueError:
            return False
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE jobs
                   SET status = 'paused',
                       assigned_agent_id = NULL,
                       freeze_data = NULL,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE id = $1
                   AND status = 'waiting'
                RETURNING id
                """,
                job_uuid,
            )
            return row is not None

    # --- Session wake outbox -------------------------------------------------
    #
    # A single-message-per-row transactional outbox on the jobs row itself:
    # same correctness as a dedicated outbox table, no new table, and durable
    # and inspectable in a way an in-memory claim is not. Design + rationale:
    # docs/features/session_wake_on_job_completion.md.
    #
    # The claim exists because the send is NOT idempotent — the agent's
    # /api/input mints a fresh message id per call and unconditionally enqueues,
    # so a duplicate is a visible message in the user's transcript plus a second
    # paid LLM turn. Claim first, commit, then send.
    #
    # Deliberately NOT modelled on claim_delegation_resume: its dedup is a CAS
    # on the waiter's own status transition (waiting → paused), where the legal
    # transition IS the already-woken flag. A thread has no equivalent — it is
    # legitimately 'active' before, during and after a wake — so the claim has
    # to be materialized.

    async def mark_job_wake_pending(self, job_id: str, terminal_status: str) -> bool:
        """Enqueue a completion wake for the session that created ``job_id``.

        Returns True iff this call moved the row into 'pending' (i.e. a wake is
        now owed). False covers every legitimate no-op: the job was not created
        by a session, its owner opted out, a wake for this same terminal status
        was already delivered, or one is already in flight.

        Idempotent by construction, which is what makes it safe to call from
        several completion paths plus the sweeper for the same job. The guard
        ``wake_state IN ('none','sent')`` is what stops an in-flight wake from
        being re-enqueued; ``wake_notified_status IS DISTINCT FROM $2`` is what
        stops the *same* terminal status from being delivered twice while still
        allowing pending_review → completed to wake a second time.

        Note the deliberate omission: this does NOT clear wake_attempts. A row
        that already burned attempts on an unreachable pod keeps its budget, so
        a permanently dead session can't be re-armed into an infinite retry by
        repeated terminal transitions.
        """
        try:
            job_uuid = UUID(job_id)
        except ValueError:
            return False
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE jobs
                   SET wake_state = 'pending',
                       wake_claimed_at = NULL
                 WHERE id = $1
                   AND wake_on_complete
                   AND created_by_thread_id IS NOT NULL
                   AND wake_state IN ('none', 'sent')
                   AND wake_notified_status IS DISTINCT FROM $2
                RETURNING id
                """,
                job_uuid,
                terminal_status,
            )
            return row is not None

    async def claim_pending_job_wakes(
        self,
        *,
        limit: int = 20,
        visibility_timeout_seconds: int = 120,
    ) -> List[Dict[str, Any]]:
        """Atomically claim up to ``limit`` owed wakes for THIS caller to send.

        Returns the claimed rows. Every returned row is exclusively ours until
        the visibility timeout expires; the caller MUST follow up with
        :meth:`finish_job_wake` or :meth:`release_job_wake` for each.

        Atomicity is guaranteed by documented Read Committed semantics, not by
        luck: ``SELECT ... FOR UPDATE`` re-evaluates its WHERE against the row
        version it actually locked, so a loser matches nothing and gets zero
        rows back. Combined with SKIP LOCKED (two replicas take disjoint sets
        rather than one blocking on the other), exactly one replica sends each
        wake.

        Three WHERE arms, each load-bearing:

        * ``wake_state = 'pending'`` — the hook fired and a wake is owed.
        * ``'sending'`` past the visibility timeout — re-claim of a row a
          replica died holding. This arm is the entire durability argument: it
          is why losing the opportunistic post-commit send is harmless, and why
          a SIGKILL mid-rollout costs latency rather than a silently-lost wake.
        * ``'none'`` on an already-terminal job — the BACKSTOP. There is no
          terminal-state choke point in this codebase: dispatch-time
          grant/config failures, the LLM-outage fail path and VM-upgrade
          approval expiry all mark a job terminal with a direct DB write and no
          hook of any kind. Finding those by *status* rather than by hook is
          what turns "a session that waits forever" into "one tick of latency".
          ``wake_notified_status IS DISTINCT FROM status`` keeps it from
          re-firing what was already delivered.

        INDEX CONTRACT: the states are LITERAL SQL here so the partial index
        idx_jobs_wake_pending (migration 0071) applies. Rewriting any of them to
        ``wake_state = ANY($n)`` makes the predicate non-immutable at plan time
        and permanently disables the index — turning every sweeper tick into a
        seq-scan of jobs. The index predicate also carries ``wake_on_complete``
        and ``created_by_thread_id IS NOT NULL``, which is why they lead the
        WHERE here rather than being left implicit.
        """
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE jobs j
                   SET wake_state = 'sending',
                       wake_claimed_at = NOW(),
                       wake_attempts = j.wake_attempts + 1
                  FROM (
                        SELECT id FROM jobs
                         WHERE wake_on_complete
                           AND created_by_thread_id IS NOT NULL
                           AND (
                                wake_state = 'pending'
                                OR (wake_state = 'sending'
                                    AND wake_claimed_at
                                        < NOW() - make_interval(
                                            secs => $1::double precision))
                                OR (wake_state = 'none'
                                    AND status IN ('completed', 'failed',
                                                   'cancelled', 'pending_review')
                                    AND wake_notified_status IS DISTINCT FROM status)
                               )
                         ORDER BY updated_at
                         FOR UPDATE SKIP LOCKED
                         LIMIT $2
                       ) s
                 WHERE j.id = s.id
                RETURNING j.id, j.created_by_thread_id, j.status, j.wake_attempts,
                          j.user_id, j.project_id, j.description, j.expert_id,
                          j.config_name, j.freeze_data, j.error_message
                """,
                float(visibility_timeout_seconds),
                limit,
            )
        return [dict(row) for row in rows]

    async def finish_job_wake(self, job_id: str, delivered_status: str) -> None:
        """Mark a claimed wake delivered, recording WHICH terminal status went
        out. The recorded status is what lets a later, different terminal state
        (approve flipping pending_review → completed) wake the session again
        without re-delivering the one it already saw."""
        try:
            job_uuid = UUID(job_id)
        except ValueError:
            return
        async with self.acquire() as conn:
            await conn.execute(
                """
                UPDATE jobs
                   SET wake_state = 'sent',
                       wake_notified_status = $2,
                       wake_claimed_at = NULL
                 WHERE id = $1
                   AND wake_state = 'sending'
                """,
                job_uuid,
                delivered_status,
            )

    async def release_job_wake(self, job_id: str, *, max_attempts: int = 8) -> str:
        """Hand a failed send back for retry, or bury it.

        Returns the resulting state ('pending' or 'dead'). Burying at the cap is
        what keeps one permanently-unreachable session from re-claiming forever
        and starving live wakes behind it in the ORDER BY. ``wake_state='dead'``
        is the single alert-worthy metric this feature has — a non-zero count
        means some session is waiting on a job it will never hear about, which
        is precisely the bug the feature exists to remove.
        """
        try:
            job_uuid = UUID(job_id)
        except ValueError:
            return "dead"
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE jobs
                   SET wake_state = CASE WHEN wake_attempts >= $2 THEN 'dead'
                                         ELSE 'pending' END,
                       wake_claimed_at = NULL
                 WHERE id = $1
                   AND wake_state = 'sending'
                RETURNING wake_state
                """,
                job_uuid,
                max_attempts,
            )
        return str(row["wake_state"]) if row else "dead"

    async def get_job_wake_stats(self) -> Dict[str, int]:
        """Fleet-wide wake-outbox depth by state.

        ``dead`` is the alert-worthy number and the whole observability story
        for this feature: each one is a session waiting on a job it will never
        be told about. Excludes 'none' — most jobs are not session-created and
        counting them would swamp the signal.
        """
        async with self.acquire() as conn:
            rows = await conn.fetch(
                "SELECT wake_state, COUNT(*) AS n FROM jobs "
                "WHERE wake_state <> 'none' GROUP BY wake_state"
            )
        counts = {str(r["wake_state"]): int(r["n"]) for r in rows}
        return {
            "pending": counts.get("pending", 0),
            "sending": counts.get("sending", 0),
            "sent": counts.get("sent", 0),
            "dead": counts.get("dead", 0),
        }

    async def get_thread_job_counts(self, thread_id: str) -> Dict[str, int]:
        """Terminal/in-flight tallies over every job a thread created.

        Feeds the "1 of 3 finished — 1 still running, 1 failed" line in the wake
        payload. Without it the agent must spend a list_worker_jobs round-trip
        on EVERY wake just to decide whether this is the moment to act; with it
        the decision is free. Keys: total, running, completed, failed,
        cancelled, pending_review.
        """
        try:
            thread_uuid = UUID(thread_id)
        except ValueError:
            return {}
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT status, COUNT(*) AS n
                  FROM jobs
                 WHERE created_by_thread_id = $1
                 GROUP BY status
                """,
                thread_uuid,
            )
        by_status = {str(r["status"]): int(r["n"]) for r in rows}
        terminal = ("completed", "failed", "cancelled")
        return {
            "total": sum(by_status.values()),
            "finished": sum(by_status.get(s, 0) for s in terminal),
            "running": sum(n for s, n in by_status.items() if s not in terminal),
            "completed": by_status.get("completed", 0),
            "failed": by_status.get("failed", 0),
            "cancelled": by_status.get("cancelled", 0),
            "pending_review": by_status.get("pending_review", 0),
        }

    async def queue_job_for_resume(
        self, job_id: str, context_merge: Dict[str, Any] | None = None
    ) -> bool:
        """Park a job as 'paused' (dispatchable) in ONE statement.

        Merges ``context_merge`` (``queued_feedback`` and friends), flips the
        status, unassigns the agent, and sheds the row-level freeze — stashing
        it in ``context.last_freeze_data`` for observability first.

        Clearing the freeze is the whole point. ``get_dispatchable_jobs``
        requires ``freeze_data IS NULL`` (partial-index contract, 0046), so a
        resume that keeps the blob leaves the job paused-but-INVISIBLE: the
        dispatcher never selects it again and nothing else is scheduled to
        change its state. A stale row-level freeze also poisons the next
        completion (``_parse_freeze_data`` prefers the DB copy over the request
        body). Every other frozen→paused transition already does this
        (``claim_delegation_resume``, ``claim_llm_outage_redispatch``, the
        vm_upgrade approve/deny arms); the human-reply resume paths did not,
        which wedged every reply to a blocking agent message.
        See docs/issues/blocking_message_reply_keeps_freeze_data.md.

        Returns True iff a row was updated.
        """
        try:
            job_uuid = UUID(job_id)
        except ValueError:
            return False
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE jobs
                   SET context = COALESCE(context, '{}'::jsonb)
                                 || $2::jsonb
                                 || CASE
                                        WHEN freeze_data IS NULL THEN '{}'::jsonb
                                        ELSE jsonb_build_object(
                                            'last_freeze_data', freeze_data
                                        )
                                    END,
                       status = 'paused',
                       assigned_agent_id = NULL,
                       freeze_data = NULL,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE id = $1
                RETURNING id
                """,
                job_uuid,
                json.dumps(context_merge or {}),
            )
            return row is not None

    async def list_due_llm_outage_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Paused ``llm_unavailable`` jobs whose backoff timer is due for re-dispatch.

        Read by the outage sweeper each tick: ``status='paused'``, agent freed,
        and a ``freeze_data.next_retry_at`` that has arrived. Oldest-due first so
        a backlog drains fairly. ``parent_job_id``/``creation_order`` ride along
        so the sweeper's ceiling-fail path can run the subjob unblock handlers
        (docs/features/llm_outage_subjob_resilience.md). See
        docs/features/llm_outage_pause_and_backoff_redispatch.md.
        """
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, config_name, context, freeze_data, user_id, project_id,
                       parent_job_id, creation_order
                FROM jobs
                WHERE status = 'paused'
                  AND assigned_agent_id IS NULL
                  AND freeze_data->>'freeze_type' = 'llm_unavailable'
                  AND freeze_data ? 'next_retry_at'
                  AND (freeze_data->>'next_retry_at')::timestamptz <= now()
                ORDER BY (freeze_data->>'next_retry_at')::timestamptz ASC
                LIMIT $1
                """,
                limit,
            )
        return [dict(row) for row in rows]

    async def claim_llm_outage_redispatch(self, job_id: str) -> bool:
        """Atomically clear an ``llm_unavailable`` freeze so the dispatcher re-queues it.

        CAS guarded on ``status='paused'`` + ``freeze_type`` + ``next_retry_at``
        due, so exactly one sweeper wins even in a transient dual-leader window
        (belt-and-suspenders with ``run_when_leader``). Sets ``freeze_data=NULL``
        (``get_dispatchable_jobs`` requires ``freeze_data IS NULL``) and re-asserts
        ``assigned_agent_id=NULL``. ``context.llm_outage`` is untouched, so the
        attempt counter survives the resume. Returns True iff THIS call cleared it.
        See docs/features/llm_outage_pause_and_backoff_redispatch.md.
        """
        try:
            uuid_val = UUID(job_id)
        except ValueError:
            return False
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE jobs
                   SET freeze_data = NULL,
                       assigned_agent_id = NULL,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE id = $1
                   AND status = 'paused'
                   AND freeze_data->>'freeze_type' = 'llm_unavailable'
                   AND (freeze_data->>'next_retry_at')::timestamptz <= now()
                RETURNING id
                """,
                uuid_val,
            )
            return row is not None

    async def fail_llm_outage_job(self, job_id: str, reason: str) -> bool:
        """Terminally fail a paused ``llm_unavailable`` job past its give-up ceiling.

        CAS guarded (``status='paused'`` + ``freeze_type``) so the sweeper
        backstop and a racing ``/complete`` can't double-fail — the loop counts
        it exactly once (the project-loop safety net advances it). Records the
        reason + stamps ``completed_at``; leaves ``freeze_data`` for the audit
        trail (a ``failed`` job is never re-dispatched). Returns True iff THIS
        call failed it. See docs/features/llm_outage_pause_and_backoff_redispatch.md.
        """
        try:
            uuid_val = UUID(job_id)
        except ValueError:
            return False
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE jobs
                   SET status = 'failed',
                       error_message = $2,
                       completed_at = NOW(),
                       updated_at = CURRENT_TIMESTAMP
                 WHERE id = $1
                   AND status = 'paused'
                   AND freeze_data->>'freeze_type' = 'llm_unavailable'
                RETURNING id
                """,
                uuid_val,
                reason,
            )
            return row is not None

    async def get_dispatchable_jobs(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get jobs waiting for assignment, ordered by priority then creation time.

        Returns jobs in 'created' (new) or 'paused' (preempted) status
        that have no assigned agent.  Excludes jobs whose ancestor chain
        contains a paused, cancelled, or failed parent (cascade guard).

        Args:
            limit: Maximum jobs to return

        Returns:
            List of job dicts ordered by priority DESC, created_at ASC

        Index contract: the WHERE terms below match the partial index
        idx_jobs_dispatchable (0046_jobs_dispatchable_partial_idx). Keep the
        statuses LITERAL — rewriting to ``status = ANY($1)`` makes the predicate
        non-immutable at plan time and permanently disables that index.
        """
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT j.id, j.description, j.status, j.config_name,
                       j.config_override, j.assigned_agent_id, j.user_id,
                       j.project_id, j.parent_job_id, j.priority, j.runner_kind,
                       j.branch_name, j.context, j.created_at
                FROM jobs j
                -- These three terms are the partial index idx_jobs_dispatchable
                -- (0046). Statuses MUST stay literal (see docstring); the
                -- ORDER BY priority DESC, created_at ASC is the index key.
                WHERE j.status IN ('created', 'paused')
                  AND j.assigned_agent_id IS NULL
                  AND j.freeze_data IS NULL
                  -- Mode A: skip jobs whose cloud-folder baseline is still
                  -- being seeded. The seed task flips this to 'ready' (or
                  -- 'failed', in which case we still dispatch so the job
                  -- doesn't deadlock; loss is just an empty diff).
                  AND COALESCE(
                      j.context->'cloud_baseline'->>'state', 'ready'
                  ) <> 'seeding'
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
                       last_completed_at, metadata, pod_uid
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
        config_name: str = "session_base",
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

    @asynccontextmanager
    async def thread_advisory_lock(self, thread_id: str):
        """Postgres advisory lock keyed by ``thread_id``.

        Pattern matches the schema-migration lock at
        ``orchestrator/database/migrate.py:157``. The lock key is a stable
        hash of the thread_id (Postgres advisory locks take a bigint key).
        Used by ``POST /api/sessions/{thread_id}/prepare`` to serialize
        concurrent prepare calls — see
        ``docs/issues/persistent_thread_double_provisioning_race.md``.
        """
        h = hashlib.blake2b(thread_id.encode(), digest_size=8).digest()
        key = int.from_bytes(h, byteorder="big", signed=True)

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock($1)", key)
                yield
                # Lock auto-released at transaction end.

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

    async def list_threads_needing_workspace(self) -> List[Dict[str, Any]]:
        """Active sessions whose workspace_container entry exists but is not ready or
        in-progress (e.g. 'failed') — candidates for the session reconcile to re-ensure.

        The positive ``status = 'active'`` filter excludes every non-active thread
        (ended, suspended, awaiting_user, ...) — i.e. anything not currently running.
        Requiring a workspace_container entry means sessions that never provisioned a
        workspace are never spuriously created.

        The excluded workspace statuses below are the "already progressing" set; keep
        them in sync with ensure_workspace's in-progress guard in
        services/workspace_lifecycle.py (plus 'ready'). Drift is low-impact: a missed
        status just gets re-selected and no-op'd by ensure_workspace.
        """
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, metadata
                FROM threads
                WHERE status = 'active'
                  AND metadata->'workspace_container' IS NOT NULL
                  AND COALESCE(metadata->'workspace_container'->>'status', '') NOT IN
                      ('ready', 'creating', 'restoring', 'created', 'pending', 'suspending')
                """,
            )
        return [dict(r) for r in rows]

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
        try:
            thread_uuid = UUID(str(thread_id))
        except (TypeError, ValueError):
            return
        async with self.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    "srw-docker-workspace-pool",
                )
                await conn.execute(
                    """
                    UPDATE docker_workspace_leases
                    SET status = 'quarantined',
                        quarantine_reason = 'owner_deleted_before_recreation',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE owner_kind = 'thread' AND owner_id = $1
                      AND status IN ('ready', 'releasing')
                    """,
                    thread_uuid,
                )
                await conn.execute(
                    "DELETE FROM thread_messages WHERE thread_id = $1",
                    thread_uuid,
                )
                await conn.execute(
                    "DELETE FROM threads WHERE id = $1",
                    thread_uuid,
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

    async def update_thread_title(self, thread_id: str, title: str) -> None:
        """Set a thread's display title (user rename).

        Deliberately does NOT touch last_activity: a rename is metadata, not
        conversational activity, and bumping it would re-sort the
        recency-ordered session list.
        """
        async with self.acquire() as conn:
            await conn.execute(
                "UPDATE threads SET title = $2 WHERE id = $1",
                thread_id,
                title,
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

    # --------------------------------------------------------------- thread_mounts
    # Canonical store for which cloud surfaces are attached to a thread.
    # Replaces the legacy ``threads.metadata.project_ids`` JSONB key as
    # source of truth for project attachment (Phase 1 of
    # docs/features/cloud_collaboration_model.md). The runtime
    # ``project_ids: list[str]`` contract for downstream consumers is
    # derived from these rows at payload-build time.

    async def add_thread_mount(
        self,
        thread_id: str,
        *,
        mount_kind: str,
        target_path: str,
        source_kind: str,
        source_ref: str | None = None,
        backend_id: str | None = None,
        cloud_handle: str | None = None,
        webdav_url: str | None = None,
        target_user_sub: str | None = None,
    ) -> str:
        """Insert one mount row and return its UUID."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO thread_mounts (
                    thread_id, mount_kind, target_path,
                    source_kind, source_ref,
                    backend_id, cloud_handle, webdav_url,
                    target_user_sub
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING id
                """,
                UUID(thread_id),
                mount_kind,
                target_path,
                source_kind,
                UUID(source_ref) if source_ref else None,
                backend_id,
                cloud_handle,
                webdav_url,
                target_user_sub,
            )
        return str(row["id"])

    async def list_thread_mounts(self, thread_id: str) -> list[Dict[str, Any]]:
        """Return all mounts attached to a thread, ordered by ``target_path``."""
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, thread_id, mount_kind, target_path,
                       source_kind, source_ref,
                       backend_id, cloud_handle, webdav_url,
                       target_user_sub, created_at
                FROM thread_mounts
                WHERE thread_id = $1
                ORDER BY target_path
                """,
                UUID(thread_id),
            )
        return [dict(row) for row in rows]

    async def list_thread_mounts_bulk(
        self, thread_ids: list[str]
    ) -> Dict[str, list[Dict[str, Any]]]:
        """All mounts for each thread, in ONE query, ordered by ``target_path``.

        Batched form of :meth:`list_thread_mounts` for the thread-list endpoint
        — replaces the per-thread N+1 with a single ``thread_id = ANY($1)``
        fetch. Returns ``{thread_id: [mount_row, ...]}``; threads with no mounts
        are absent. Invalid UUIDs are skipped.
        """
        valid: list[UUID] = []
        for t in thread_ids:
            try:
                valid.append(UUID(str(t)))
            except (ValueError, TypeError):
                continue
        if not valid:
            return {}

        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, thread_id, mount_kind, target_path,
                       source_kind, source_ref,
                       backend_id, cloud_handle, webdav_url,
                       target_user_sub, created_at
                FROM thread_mounts
                WHERE thread_id = ANY($1::uuid[])
                ORDER BY thread_id, target_path
                """,
                valid,
            )
        out: Dict[str, list[Dict[str, Any]]] = {}
        for row in rows:
            out.setdefault(str(row["thread_id"]), []).append(dict(row))
        return out

    async def remove_thread_mount(self, mount_id: str) -> bool:
        """Delete a single mount by id. Returns True if a row was removed."""
        async with self.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM thread_mounts WHERE id = $1",
                UUID(mount_id),
            )
        return result.endswith(" 1")

    async def replace_thread_mounts(
        self,
        thread_id: str,
        mounts: list[Dict[str, Any]],
    ) -> list[str]:
        """Atomically replace a thread's mount set.

        Each entry in ``mounts`` must carry ``mount_kind``, ``target_path``,
        ``source_kind``; ``source_ref`` / ``backend_id`` / ``cloud_handle``
        / ``webdav_url`` are optional. Returns the list of new mount IDs in
        the order they were inserted.
        """
        new_ids: list[str] = []
        async with self.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM thread_mounts WHERE thread_id = $1",
                    UUID(thread_id),
                )
                for entry in mounts:
                    src_ref = entry.get("source_ref")
                    row = await conn.fetchrow(
                        """
                        INSERT INTO thread_mounts (
                            thread_id, mount_kind, target_path,
                            source_kind, source_ref,
                            backend_id, cloud_handle, webdav_url,
                            target_user_sub
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        RETURNING id
                        """,
                        UUID(thread_id),
                        entry["mount_kind"],
                        entry["target_path"],
                        entry["source_kind"],
                        UUID(src_ref) if src_ref else None,
                        entry.get("backend_id"),
                        entry.get("cloud_handle"),
                        entry.get("webdav_url"),
                        entry.get("target_user_sub"),
                    )
                    new_ids.append(str(row["id"]))
        return new_ids

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

    async def mark_orphaned_threads_suspended(self) -> list[str]:
        """Suspend PAUSED threads whose bound agent went offline.

        Companion to ``mark_orphaned_threads_ended`` for the ``awaiting_user``
        / ``suspended`` states that method intentionally skips. A thread only
        reaches these states after an agent ran a turn, so a non-NULL
        ``agent_id`` here always pointed at a once-live agent; if that agent is
        now ``offline`` the binding is stale and every reopen path wedges on it
        (GET /connection 409-loops because the cockpit only re-provisions on a
        425). Clearing ``agent_id`` + setting ``suspended`` routes the next open
        through the normal re-provision path (``_do_prepare`` has no status
        gate) and suspended-restore (which keys off
        ``metadata.workspace_container.status``, not thread status).

        Set-based CAS: idempotent across ticks (once ``agent_id`` is NULL the
        row no longer matches) and race-free against a concurrent ``/prepare``
        (which only ever re-binds to a live, non-offline agent). No advisory
        lock needed — same contract as the drain-suspend CAS in ``main.py``.

        Returns:
            List of thread IDs suspended. The caller is responsible for freeing
            the associated workspace + agent pod; this method only owns the DB
            state transition.
        """
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE threads
                SET status        = 'suspended',
                    agent_id      = NULL,
                    last_activity = CURRENT_TIMESTAMP
                WHERE status IN ('awaiting_user', 'suspended')
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

    async def mark_stalled_working_agents_by_graph_progress(
        self, stall_minutes: int = 10
    ) -> int:
        """Pause agents that keep heartbeating without graph-progress updates.

        This catches a specific wedge mode: the process still reports heartbeats
        and stays in ``working`` state, but its tool/graph progress marker has
        not changed for a sustained interval.

        Args:
            stall_minutes: Time without a progress-marker timestamp bump before
                the agent is released to ``ready``.

        Returns:
            Number of agents flipped to ``ready``.
        """
        if stall_minutes <= 0:
            return 0

        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE agents
                SET status = 'ready'
                WHERE status = 'working'
                  AND current_job_id IS NOT NULL
                  AND metadata ? 'graph_progress_seen_at'
                  AND (metadata->>'graph_progress_seen_at')::timestamptz
                        < NOW() - make_interval(mins => $1)
                """,
                stall_minutes,
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

    async def reap_orphaned_session_agents(
        self, grace_minutes: int = 5
    ) -> List[Dict[str, Any]]:
        """Find live session agents wedged with no bound thread or job.

        STOPGAP (2026-06-23). Companion to ``mark_stuck_session_agents_ready``
        for the one case that sweep cannot fix: an agent stuck at
        ``status='session'`` with ``thread_id IS NULL``. That sweep's predicate
        requires ``thread_id IS NOT NULL``, so once the binding is gone it can
        never match. And because the agent re-asserts ``'session'`` on every 5s
        heartbeat (``persistent_app._heartbeat_status``), flipping the row to
        ``'ready'`` does not stick — the only actuation that does is deleting
        the pod. This method returns the pods to delete; the caller
        (``stale_agent_detector``) issues the K8s delete, after which the row
        ages to ``offline`` (heartbeat timeout) and is GC'd normally.

        Safe by construction: ``thread_id IS NULL AND current_job_id IS NULL``
        means the agent holds nothing user-visible. A thread-bound live session
        keeps ``thread_id`` set and is never selected — this does NOT re-open
        the 2026-06-10 drift-drain-kills-idle-sessions incident.

        Grace: the orphan state must persist >= ``grace_minutes``. First
        sighting stamps ``intents.session_orphaned_at = NOW()`` (DB clock); the
        row is only returned once that stamp is older than the grace. The stamp
        is cleared as soon as the agent leaves the orphan state (rebound to a
        thread/job, or flipped ready/offline), so the brief attach window where
        ``status`` is 'session' before ``thread_id`` is written is never reaped.

        Superseded by the orchestrator intent/observed split in
        ``docs/features/unified_instance_lifecycle.md``; tracked in
        ``docs/issues/lifecycle_session_agents_without_thread_never_drain.md``.

        Returns:
            List of ``{"id": <agent uuid str>, "hostname": <pod name>}`` for
            pods the caller must delete.
        """
        orphan_pred = (
            "status = 'session' AND thread_id IS NULL AND current_job_id IS NULL"
        )
        async with self.acquire() as conn:
            # Clear the stamp on any row that recovered (rebound to a
            # thread/job, or left 'session') so a future wedge re-arms the grace.
            await conn.execute(
                f"""
                UPDATE agents
                SET intents = intents - 'session_orphaned_at'
                WHERE COALESCE(intents, '{{}}'::jsonb) ? 'session_orphaned_at'
                  AND NOT ({orphan_pred})
                """
            )
            # Stamp first sighting of the orphan state (DB clock — no app clock).
            await conn.execute(
                f"""
                UPDATE agents
                SET intents = COALESCE(intents, '{{}}'::jsonb)
                              || jsonb_build_object('session_orphaned_at', to_jsonb(NOW()))
                WHERE {orphan_pred}
                  AND NOT (COALESCE(intents, '{{}}'::jsonb) ? 'session_orphaned_at')
                """
            )
            # Select orphans whose stamp is older than the grace — reap these.
            rows = await conn.fetch(
                f"""
                SELECT id, hostname
                FROM agents
                WHERE {orphan_pred}
                  AND hostname IS NOT NULL
                  AND (COALESCE(intents, '{{}}'::jsonb) ->> 'session_orphaned_at')::timestamptz
                      < NOW() - make_interval(mins => $1)
                """,
                grace_minutes,
            )
        return [{"id": str(row["id"]), "hostname": row["hostname"]} for row in rows]

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
        reasoning: Optional[Any] = None,
        tool_results: Optional[Any] = None,
        provider: Optional[str] = None,
        provider_raw: Optional[Any] = None,
        additional_kwargs: Optional[dict] = None,
        response_metadata: Optional[dict] = None,
    ) -> str:
        """Save a message to thread_messages. Fire-and-forget safe.

        ``tool_call_id`` is set only on role='tool' rows and links the result
        back to the AIMessage's tool_calls[].id. ``thinking`` is set only on
        role='ai' rows that carry reasoning content. See migration 0011.
        Rows are append-only; the component columns (reasoning, tool_results,
        provider, provider_raw, additional_kwargs, response_metadata) are
        nullable and were added in migration 0019.
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO thread_messages
                    (thread_id, role, content, tool_calls, turn_number,
                     metrics, tool_call_id, thinking,
                     reasoning, tool_results, provider, provider_raw,
                     additional_kwargs, response_metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                RETURNING id
                """,
                thread_id,
                role,
                content,
                json.dumps(tool_calls) if tool_calls is not None else None,
                turn_number,
                json.dumps(metrics) if metrics is not None else None,
                tool_call_id,
                thinking,
                json.dumps(reasoning) if reasoning is not None else None,
                json.dumps(tool_results) if tool_results is not None else None,
                provider,
                json.dumps(provider_raw) if provider_raw is not None else None,
                json.dumps(additional_kwargs)
                if additional_kwargs is not None
                else None,
                json.dumps(response_metadata)
                if response_metadata is not None
                else None,
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

    @staticmethod
    def _thread_message_to_dict(row: Any) -> Dict[str, Any]:
        """Map a ``thread_messages`` row to the cockpit display shape."""
        return {
            "id": str(row["id"]),
            "role": row["role"],
            "content": row["content"],
            "tool_calls": json.loads(row["tool_calls"]) if row["tool_calls"] else None,
            "turn_number": row["turn_number"],
            "metrics": json.loads(row["metrics"]) if row["metrics"] else None,
            "tool_call_id": row["tool_call_id"],
            "thinking": row["thinking"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }

    async def get_thread_messages_history(
        self,
        thread_id: str,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Load thread message history for the cockpit display path, ascending.

        ``limit=None`` (the default) loads the **entire** conversation. The
        cockpit caches the full thread client-side and windows the render
        itself, so the display path must not truncate. The old default of 200
        sliced long threads to their oldest 200 rows — the "only the first
        message shows" bug; the agent copy in ``src/database/postgres_db.py``
        was fixed earlier but this display copy was missed.

        Pass an explicit ``limit``/``offset`` for the legacy oldest-first paged
        read still used by the MCP inspection tool. For backfill / reconnect
        catch-up use ``get_thread_messages_page``.

        Ordering is ``created_at ASC`` with ``turn_number, id`` tiebreakers so
        parallel tool calls written in the same instant stay deterministic.
        """
        query = (
            "SELECT id, role, content, tool_calls, turn_number, metrics, "
            "tool_call_id, thinking, created_at FROM thread_messages "
            "WHERE thread_id = $1 "
            "ORDER BY created_at ASC, turn_number ASC, id ASC"
        )
        params: List[Any] = [thread_id]
        if limit is not None:
            params.append(limit)
            query += f" LIMIT ${len(params)}"
            params.append(offset)
            query += f" OFFSET ${len(params)}"
        async with self.acquire() as conn:
            rows = await conn.fetch(query, *params)
        return [self._thread_message_to_dict(r) for r in rows]

    async def get_thread_messages_page(
        self,
        thread_id: str,
        before: Optional[datetime] = None,
        after: Optional[datetime] = None,
        limit: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """Cursor-paged thread message read for the cockpit display (Phase 2).

        Supply exactly one of ``before`` / ``after``:

        - ``before``: backfill — the newest rows at-or-before the cursor, up to
          ``limit``, returned ascending.
        - ``after``: catch-up — rows at-or-after the cursor, up to ``limit``,
          ascending.

        Cursor bounds are **inclusive**; the cockpit dedups by ``id``, so rows
        that tie on ``created_at`` at the window edge are never dropped. Returns
        ``(messages_ascending, has_more)`` — ``has_more`` is whether further
        rows exist beyond the window in the paging direction (older for
        ``before``, newer for ``after``).
        """
        clauses = ["thread_id = $1"]
        params: List[Any] = [thread_id]
        if before is not None:
            params.append(before)
            clauses.append(f"created_at <= ${len(params)}")
            descending = True
        elif after is not None:
            params.append(after)
            clauses.append(f"created_at >= ${len(params)}")
            descending = False
        else:
            descending = False

        order = (
            "created_at DESC, turn_number DESC, id DESC"
            if descending
            else "created_at ASC, turn_number ASC, id ASC"
        )
        query = (
            "SELECT id, role, content, tool_calls, turn_number, metrics, "
            "tool_call_id, thinking, created_at FROM thread_messages "
            f"WHERE {' AND '.join(clauses)} ORDER BY {order}"
        )
        if limit is not None:
            params.append(limit + 1)  # probe one extra row to detect has_more
            query += f" LIMIT ${len(params)}"

        async with self.acquire() as conn:
            rows = list(await conn.fetch(query, *params))

        has_more = False
        if limit is not None and len(rows) > limit:
            has_more = True
            rows = rows[:limit]
        if descending:
            rows.reverse()  # normalize to ascending for display
        return [self._thread_message_to_dict(r) for r in rows], has_more

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
                       config, cli_hint, default_branch, job_id, created_by, is_global,
                       read_only, created_at, updated_at
                FROM datasources
                {where_clause}
                ORDER BY created_at DESC
                LIMIT ${param_count}
                """,
                *values,
            )

        return [_datasource_row_to_dict(row) for row in rows]

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
                       config, cli_hint, default_branch, job_id, created_by, is_global,
                       read_only, created_at, updated_at
                FROM datasources
                WHERE id = $1
                """,
                uuid_val,
            )

        return _datasource_row_to_dict(row) if row else None

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
        read_only: bool | None = None,
        config: Dict[str, Any] | None = None,
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
            read_only: Declared read-only flag for public datasources
                       (None = not applicable; declarative only)
            config: Non-secret type-specific configuration

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
                                         credentials, config, job_id, cli_hint,
                                         default_branch, created_by, is_global,
                                         read_only)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11, $12)
                RETURNING id, name, description, type, connection_url, credentials,
                          config, job_id, cli_hint, default_branch, created_by, is_global,
                          read_only, created_at, updated_at
                """,
                name,
                description,
                ds_type,
                connection_url,
                _encrypt_credentials_dict(credentials),
                json.dumps(config or {}),
                job_uuid,
                cli_hint,
                default_branch,
                owner_uuid,
                is_global,
                read_only,
            )

        return _datasource_row_to_dict(row)

    async def update_datasource(
        self,
        datasource_id: str,
        name: str | None = None,
        description: str | None = None,
        connection_url: str | None = None,
        credentials: Dict[str, Any] | None = None,
        cli_hint: str | None = None,
        default_branch: str | None = None,
        config: Dict[str, Any] | None = None,
        is_global: bool | None = None,
        read_only: bool | None = None,
        connection_url_set: bool = False,
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
            config: New non-secret type-specific configuration
            is_global: Publish (True) / unpublish (False); None = unchanged
            read_only: Declared read-only flag; None = unchanged
            connection_url_set: Persist ``connection_url`` even when it is
                explicitly ``None`` (used when an MCP switches to stdio).

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

        if connection_url is not None or connection_url_set:
            param_count += 1
            updates.append(f"connection_url = ${param_count}")
            values.append(connection_url)

        if credentials is not None:
            param_count += 1
            updates.append(f"credentials = ${param_count}")
            values.append(_encrypt_credentials_dict(credentials))

        if cli_hint is not None:
            param_count += 1
            updates.append(f"cli_hint = ${param_count}")
            values.append(cli_hint)

        if default_branch is not None:
            param_count += 1
            updates.append(f"default_branch = ${param_count}")
            values.append(default_branch)

        if config is not None:
            param_count += 1
            updates.append(f"config = ${param_count}::jsonb")
            values.append(json.dumps(config))

        # `is not None` keeps explicit False working — unpublish and
        # RW-declare both pass False.
        if is_global is not None:
            param_count += 1
            updates.append(f"is_global = ${param_count}")
            values.append(is_global)

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

    async def _stamp_email_autonomous_send(
        self, datasources: List[Dict[str, Any]]
    ) -> None:
        """Dispatch-time fail-closed re-check of the email_autonomous_send grant.

        ``config.unattended_send`` is validated against the owner's grant at
        create/update, but the grant may be revoked afterwards. Stamp resolved
        email rows with ``_owner_can_autonomous_send`` so the (sync) dispatch
        payload builder can force the flag off without a DB round-trip; a row
        that never got stamped reads as no-grant (fail closed).
        """
        for ds in datasources:
            if ds.get("type") != "email":
                continue
            config = ds.get("config") or {}
            if not config.get("unattended_send"):
                continue
            allowed = False
            owner_id = ds.get("created_by")
            if owner_id:
                try:
                    owner = await self.get_user(str(owner_id))
                    if owner:
                        allowed = await self.user_can_autonomous_send(owner)
                except Exception:
                    logger.exception(
                        "email_autonomous_send owner check failed for datasource "
                        "%s; denying unattended send",
                        ds.get("id"),
                    )
            ds["_owner_can_autonomous_send"] = allowed

    async def resolve_datasources_for_job(
        self, job_id: str, project_id: str | None = None
    ) -> List[Dict[str, Any]]:
        """Resolve datasources for a job — explicit selection only.

        Returns exactly the datasources explicitly attached to the job via the
        ``job_datasources`` junction (the picker is the source of truth).
        Nothing is auto-attached: project-linked and global datasources reach a
        job only if they were selected (and so written to ``job_datasources``).

        ``project_id`` is used solely to surface the project-level ``read_only``
        setting (CLI vs tools mode) for a selected datasource that is also
        linked to that project; it never widens the result set.

        Transitional: jobs created before the explicit-only migration (0026)
        persisted their selection by cloning rows with ``datasources.job_id``
        set. If a job has no ``job_datasources`` rows, fall back to those legacy
        clones so in-flight jobs keep resolving. Remove once no live job
        predates 0026.

        Args:
            job_id: Job UUID
            project_id: Optional project UUID (only for project_read_only)

        Returns:
            List of resolved datasource dicts (may contain multiple per type)
        """
        try:
            job_uuid = UUID(job_id)
        except ValueError:
            return []

        project_uuid = UUID(project_id) if project_id else None

        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT d.id, d.name, d.description, d.type,
                    d.connection_url, d.credentials, d.config,
                    d.cli_hint, d.default_branch, d.read_only,
                    d.created_by, d.created_at, d.updated_at,
                    pd.read_only AS project_read_only
                FROM job_datasources jd
                JOIN datasources d ON d.id = jd.datasource_id
                LEFT JOIN project_datasources pd
                    ON pd.datasource_id = d.id AND pd.project_id = $2::uuid
                WHERE jd.job_id = $1
                ORDER BY d.type, d.name
                """,
                job_uuid,
                project_uuid,
            )
            if not rows:
                # Transitional fallback: legacy clone-to-job-scoped rows (pre-0026).
                rows = await conn.fetch(
                    """
                    SELECT DISTINCT d.id, d.name, d.description, d.type,
                        d.connection_url, d.credentials, d.config,
                        d.cli_hint, d.default_branch, d.read_only,
                        d.created_by, d.created_at, d.updated_at,
                        NULL::boolean AS project_read_only
                    FROM datasources d
                    WHERE d.job_id = $1
                      AND d.type <> 'kb'
                    ORDER BY d.type, d.name
                    """,
                    job_uuid,
                )

        resolved = [_datasource_row_to_dict(row) for row in rows]
        await self._stamp_email_autonomous_send(resolved)
        return resolved

    async def resolve_datasources_for_thread(
        self,
        datasource_ids: list[str] | None = None,
        project_ids: list[str] | None = None,
    ) -> List[Dict[str, Any]]:
        """Resolve datasources for a persistent thread — explicit selection only.

        Returns exactly the datasources explicitly attached to the thread by ID
        (the picker is the source of truth, persisted in
        ``threads.metadata.datasource_ids``). Nothing is auto-attached: global
        and project-linked datasources reach a thread only if they were
        selected.

        ``project_ids`` is used solely to surface the project-level ``read_only``
        setting for a selected datasource that is also linked to one of those
        projects; it never widens the result set.

        Args:
            datasource_ids: Explicit datasource UUIDs attached to the thread
            project_ids: Project UUIDs scoped to the thread (for project_read_only)

        Returns:
            List of resolved datasource dicts (may contain multiple per type)
        """
        ds_uuids = []
        for ds_id in datasource_ids or []:
            try:
                ds_uuids.append(UUID(ds_id))
            except ValueError:
                pass

        if not ds_uuids:
            return []

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
                    d.connection_url, d.credentials, d.config,
                    d.cli_hint, d.default_branch, d.read_only,
                    d.created_by, d.created_at, d.updated_at,
                    pd.read_only AS project_read_only
                FROM datasources d
                LEFT JOIN project_datasources pd
                    ON pd.datasource_id = d.id
                   AND pd.project_id = ANY($2::uuid[])
                WHERE d.id = ANY($1::uuid[])
                ORDER BY d.type, d.name
                """,
                ds_uuids,
                proj_uuids,
            )

        resolved = [_datasource_row_to_dict(row) for row in rows]
        await self._stamp_email_autonomous_send(resolved)
        return resolved

    # -- Job ↔ Datasource junction (N:M) --------------------------------------

    async def link_datasource_to_job(self, job_id: str, datasource_id: str) -> bool:
        """Link a datasource to a job (the explicit picker selection).

        The selection is persisted here; ``resolve_datasources_for_job``
        returns exactly these links. Idempotent.

        Returns True on success, False on a malformed UUID.
        """
        try:
            j_uuid = UUID(job_id)
            d_uuid = UUID(datasource_id)
        except ValueError:
            return False

        async with self.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO job_datasources (job_id, datasource_id)
                VALUES ($1, $2)
                ON CONFLICT (job_id, datasource_id) DO NOTHING
                """,
                j_uuid,
                d_uuid,
            )
        return True

    async def list_job_datasource_ids(self, job_id: str) -> List[str]:
        """Return the datasource IDs explicitly attached to a job.

        Reads the ``job_datasources`` junction; falls back to legacy
        clone-to-job-scoped rows (``datasources.job_id``) for jobs created
        before migration 0026 so parent inheritance keeps working during the
        transition.
        """
        try:
            j_uuid = UUID(job_id)
        except ValueError:
            return []

        async with self.acquire() as conn:
            rows = await conn.fetch(
                "SELECT datasource_id FROM job_datasources WHERE job_id = $1",
                j_uuid,
            )
            if rows:
                return [str(r["datasource_id"]) for r in rows]
            legacy = await conn.fetch(
                "SELECT id FROM datasources WHERE job_id = $1",
                j_uuid,
            )
        return [str(r["id"]) for r in legacy]

    async def list_eligible_datasources(
        self,
        user_id: str | None,
        project_ids: list[str] | None = None,
        *,
        is_admin: bool = False,
    ) -> List[Dict[str, Any]]:
        """Datasources a user may pre-select for a job/session (picker source).

        Union of: datasources the user created, global datasources, and
        datasources linked to any of ``project_ids``. Excludes legacy
        job-scoped clones (``datasources.job_id`` set). Admins see all
        non-job-scoped datasources. Credentials are decrypted here; callers
        must redact before returning over REST.
        """
        try:
            u_uuid = UUID(user_id) if user_id else None
        except ValueError:
            u_uuid = None

        proj_uuids = []
        for pid in project_ids or []:
            try:
                proj_uuids.append(UUID(pid))
            except ValueError:
                pass

        select_cols = """
            SELECT DISTINCT d.id, d.name, d.description, d.type,
                d.connection_url, d.credentials, d.config, d.cli_hint,
                d.default_branch, d.created_by, d.is_global, d.read_only,
                d.created_at, d.updated_at
        """

        async with self.acquire() as conn:
            if is_admin:
                rows = await conn.fetch(
                    select_cols
                    + """
                    FROM datasources d
                    WHERE d.job_id IS NULL
                    ORDER BY d.type, d.name
                    """,
                )
            else:
                rows = await conn.fetch(
                    select_cols
                    + """
                    FROM datasources d
                    LEFT JOIN project_datasources pd
                        ON pd.datasource_id = d.id
                       AND pd.project_id = ANY($2::uuid[])
                    WHERE d.job_id IS NULL
                      AND (
                          d.created_by = $1
                          OR d.is_global = true
                          OR pd.project_id IS NOT NULL
                      )
                    ORDER BY d.type, d.name
                    """,
                    u_uuid,
                    proj_uuids,
                )

        return [_datasource_row_to_dict(row) for row in rows]

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
                       d.type, d.connection_url, d.credentials, d.config,
                       d.cli_hint, d.default_branch,
                       d.job_id, d.created_by, d.is_global, d.read_only,
                       d.created_at, d.updated_at,
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

        return [_datasource_row_to_dict(row) for row in rows]

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

    async def list_datasource_projects_bulk(
        self, datasource_ids: List[str]
    ) -> Dict[str, List[str]]:
        """Project IDs linked to each datasource, in ONE query.

        Batched form of :meth:`list_datasource_projects` for the list-view
        visibility filter — replaces the per-row N+1 with a single
        ``datasource_id = ANY($1)`` fetch. Returns
        ``{datasource_id: [project_id, ...]}`` with an entry only for
        datasources that have at least one link; unlinked ids are simply absent.
        Invalid UUIDs are skipped.
        """
        valid: List[UUID] = []
        for d in datasource_ids:
            try:
                valid.append(UUID(str(d)))
            except (ValueError, TypeError):
                continue
        if not valid:
            return {}

        async with self.acquire() as conn:
            rows = await conn.fetch(
                "SELECT datasource_id, project_id FROM project_datasources "
                "WHERE datasource_id = ANY($1::uuid[])",
                valid,
            )

        out: Dict[str, List[str]] = {}
        for row in rows:
            out.setdefault(str(row["datasource_id"]), []).append(str(row["project_id"]))
        return out

    async def backfill_encrypt_datasource_credentials(self) -> Dict[str, int]:
        """One-shot migration: encrypt any plaintext ``credentials`` JSONB values.

        Idempotent — rows whose credentials column already contains a v1
        ciphertext string are skipped. Empty/null credentials are also
        skipped. Legacy plaintext dicts are re-serialized and encrypted via
        :func:`_encrypt_credentials_dict`.

        Returns counts: ``{"encrypted": N, "skipped": M, "errors": K}``.
        """
        encrypted = 0
        skipped = 0
        errors = 0

        async with self.acquire() as conn:
            rows = await conn.fetch("SELECT id, credentials FROM datasources")

            for row in rows:
                ds_id = row["id"]
                raw = row["credentials"]

                if raw is None:
                    skipped += 1
                    continue

                # asyncpg returns JSONB as a raw JSON string here.
                try:
                    parsed = json.loads(raw) if isinstance(raw, str) else raw
                except (json.JSONDecodeError, ValueError) as exc:
                    logger.error(
                        "Backfill: failed to parse credentials JSON for "
                        "datasource %s: %s",
                        ds_id,
                        exc,
                    )
                    errors += 1
                    continue

                # Already encrypted (JSON-string form with v1: prefix).
                if isinstance(parsed, str) and is_encrypted(parsed):
                    skipped += 1
                    continue

                # Empty dict — nothing to encrypt, schema default applies.
                if isinstance(parsed, dict) and not parsed:
                    skipped += 1
                    continue

                # Legacy plaintext dict — encrypt in place.
                if isinstance(parsed, dict):
                    try:
                        new_value = _encrypt_credentials_dict(parsed)
                        await conn.execute(
                            "UPDATE datasources SET credentials = $1 WHERE id = $2",
                            new_value,
                            ds_id,
                        )
                        encrypted += 1
                    except Exception as exc:
                        logger.error(
                            "Backfill: encryption failed for datasource %s: %s",
                            ds_id,
                            exc,
                        )
                        errors += 1
                    continue

                # Anything else — bare string without v1: prefix, etc.
                logger.warning(
                    "Backfill: unexpected credentials value for datasource %s "
                    "(type=%s); leaving untouched",
                    ds_id,
                    type(parsed).__name__,
                )
                errors += 1

        return {"encrypted": encrypted, "skipped": skipped, "errors": errors}

    async def backfill_strip_thread_config_secrets(self) -> Dict[str, int]:
        """One-shot migration: strip plaintext secrets from
        ``threads.metadata.config_override``.

        Persistent-session credentials are injected in-flight at session attach /
        resume and must never be stored (see
        ``security.access.redact_config_override`` and
        ``main._inject_thread_dispatch_credentials``). This removes any legacy
        plaintext keys (api_key, ``env_keys.*_API_KEY``, rclone_spec, ...) left
        in the JSONB by earlier code.

        Idempotent — a config_override that already has no secret fields is
        rewritten to an identical value, detected by equality and counted as
        skipped (no UPDATE issued).

        Returns counts: ``{"stripped": N, "skipped": M, "errors": K}``.
        """
        # Lazy import keeps the module graph acyclic (security.access pulls in
        # security.auth; this is a one-shot startup path, not a hot path).
        from security.access import redact_config_override

        stripped = 0
        skipped = 0
        errors = 0

        async with self.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, metadata FROM threads "
                "WHERE metadata -> 'config_override' IS NOT NULL"
            )

            for row in rows:
                thread_id = row["id"]
                raw = row["metadata"]

                try:
                    md = json.loads(raw) if isinstance(raw, str) else raw
                except (json.JSONDecodeError, ValueError) as exc:
                    logger.error(
                        "Strip backfill: failed to parse metadata for thread %s: %s",
                        thread_id,
                        exc,
                    )
                    errors += 1
                    continue

                co = md.get("config_override") if isinstance(md, dict) else None
                if not isinstance(co, dict):
                    skipped += 1
                    continue

                cleaned = redact_config_override(co)
                if cleaned == co:
                    skipped += 1
                    continue

                try:
                    await conn.execute(
                        """
                        UPDATE threads
                        SET metadata = jsonb_set(
                            COALESCE(metadata, '{}'::jsonb),
                            '{config_override}',
                            $1::jsonb
                        )
                        WHERE id = $2
                        """,
                        json.dumps(cleaned),
                        thread_id,
                    )
                    stripped += 1
                except Exception as exc:
                    logger.error(
                        "Strip backfill: update failed for thread %s: %s",
                        thread_id,
                        exc,
                    )
                    errors += 1

        return {"stripped": stripped, "skipped": skipped, "errors": errors}

    async def upsert_default_datasource(
        self,
        name: str,
        ds_type: str,
        connection_url: str,
        credentials: Dict[str, Any] | None = None,
        config: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Create or update a system-seeded datasource (created_by=NULL).

        Used during init to seed default datasources from env vars.
        These are always global (is_global=TRUE) and have no owner.

        Args:
            name: Datasource label
            ds_type: Datasource type
            connection_url: Connection URL
            credentials: Additional auth details
            config: Non-secret type-specific configuration

        Returns:
            Created or updated datasource dict
        """
        creds_json = _encrypt_credentials_dict(credentials)

        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO datasources (name, type, connection_url, credentials,
                                         config, created_by, is_global, read_only)
                VALUES ($1, $2, $3, $4, $5::jsonb, NULL, TRUE, TRUE)
                ON CONFLICT (name, type, COALESCE(created_by, '00000000-0000-0000-0000-000000000000'))
                DO UPDATE SET
                    connection_url = EXCLUDED.connection_url,
                    credentials = EXCLUDED.credentials,
                    config = EXCLUDED.config,
                    is_global = TRUE,
                    read_only = TRUE
                RETURNING id, name, description, type, connection_url, credentials,
                          config, created_by, is_global, read_only, created_at,
                          updated_at
                """,
                name,
                ds_type,
                connection_url,
                creds_json,
                json.dumps(config or {}),
            )

        return _datasource_row_to_dict(row)

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
    # LLM ENDPOINT TRANSPORT LOOKUP
    # Dispatch-time fetch of an endpoint row (base_url + api_key) by id, used
    # by the model resolver / credential injector. The user-facing endpoint
    # CRUD was removed with the Settings → LLM Endpoints page; custom endpoints
    # are admin-only now (Admin → Providers + Admin → Models).
    # =========================================================================

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

    # The user-side endpoint CRUD (create/update/delete) and endpoint-model
    # accessors were removed when the admin-curated `models` catalog became the
    # single source of truth and the Settings → LLM Endpoints page was retired.
    # User-scoped endpoints survive as transports only; offerings are resolved
    # via PostgresDB.resolve_catalog_model (system-scoped rows only). System
    # endpoint CRUD lives in the *_system_llm_endpoint methods below.

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

    # --- Prompt overrides (DB-backed prompt editor, v1) ----------------------

    async def list_config_overrides(self) -> List[Dict[str, Any]]:
        """List all prompt overrides (global rows first, then by family)."""
        rows = await self.fetch(
            "SELECT * FROM config_overrides ORDER BY family NULLS FIRST, kind, name"
        )
        return [dict(r) for r in rows]

    async def get_config_override(self, override_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a single prompt override by id, or None."""
        row = await self.fetchrow(
            "SELECT * FROM config_overrides WHERE id = $1", UUID(str(override_id))
        )
        return dict(row) if row else None

    async def upsert_config_override(
        self,
        *,
        family: str | None,
        kind: str,
        name: str,
        content: str | None = None,
        content_format: str | None = "text",
        value_json: Any = None,
        notes: str | None = None,
        user_id: Any = None,
    ) -> Dict[str, Any]:
        """Create or replace the override for (family, kind, name).

        Text kinds (prompts, instructions) populate ``content``; structured kinds
        (settings, guardrails) populate ``value_json``. The conflict target is the
        ``uq_config_override`` expression index ``(COALESCE(family,''), kind,
        name)``. ``created_by`` and ``updated_by`` are both set to the acting user
        on insert; only ``updated_by`` changes on update.
        """
        vj = json.dumps(value_json) if value_json is not None else None
        row = await self.fetchrow(
            """
            INSERT INTO config_overrides
                (family, kind, name, content, content_format, value_json, notes,
                 created_by, updated_by)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $8)
            ON CONFLICT (COALESCE(family, ''), kind, name) DO UPDATE
            SET content = EXCLUDED.content,
                content_format = EXCLUDED.content_format,
                value_json = EXCLUDED.value_json,
                notes = EXCLUDED.notes,
                updated_by = EXCLUDED.updated_by,
                updated_at = CURRENT_TIMESTAMP
            RETURNING *
            """,
            family,
            kind,
            name,
            content,
            content_format,
            vj,
            notes,
            UUID(str(user_id)) if user_id else None,
        )
        return dict(row)

    async def delete_config_override(self, override_id: str) -> bool:
        """Delete a prompt override by id. Returns True iff a row was removed."""
        result = await self.execute(
            "DELETE FROM config_overrides WHERE id = $1", UUID(str(override_id))
        )
        return result == "DELETE 1"

    # ── Experts (User-Defined Experts, Slice 1) ───────────────────────────

    async def create_expert(
        self,
        *,
        name: str,
        display_name: str,
        expert_type: str,
        owner_id: str,
        description: str | None = None,
        icon: str = "smart_toy",
        color: str = "#6B7280",
        tags: List[str] | None = None,
        config: Dict[str, Any] | None = None,
        prompts: Dict[str, Any] | None = None,
        is_global: bool = False,
    ) -> Dict[str, Any]:
        """Insert an owned expert. (name, owner_id) is unique — a personal fork
        named 'scholar' shadows the bundled one for that user only (decision 5)."""
        row = await self.fetchrow(
            """
            INSERT INTO experts
                (name, display_name, description, icon, color, tags, expert_type,
                 config, prompts, owner_id, is_global)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb, $10, $11)
            RETURNING *
            """,
            name,
            display_name,
            description,
            icon,
            color,
            tags or [],
            expert_type,
            json.dumps(config or {}),
            json.dumps(prompts or {}),
            UUID(str(owner_id)),
            is_global,
        )
        return dict(row)

    async def get_expert_by_id(self, expert_id: str) -> Dict[str, Any] | None:
        row = await self.fetchrow(
            "SELECT * FROM experts WHERE id = $1", UUID(str(expert_id))
        )
        return dict(row) if row else None

    async def get_expert_by_managed_key(
        self, managed_key: str
    ) -> Dict[str, Any] | None:
        row = await self.fetchrow(
            "SELECT * FROM experts WHERE managed_key = $1", managed_key
        )
        return dict(row) if row else None

    async def upsert_managed_expert(
        self,
        *,
        managed_key: str,
        seed_version: int,
        name: str,
        display_name: str,
        expert_type: str,
        description: str | None = None,
        icon: str = "smart_toy",
        color: str = "#6B7280",
        tags: List[str] | None = None,
        config: Dict[str, Any] | None = None,
        prompts: Dict[str, Any] | None = None,
    ) -> tuple[Dict[str, Any], bool]:
        """Insert a platform-managed seed expert without overwriting edits.

        ``managed_key`` is the immutable identity; display name and content are
        deliberately insert-only.  Upgrades may ship a newer seed template, but
        an operator's DB copy remains authoritative until an explicit reset
        workflow is requested.
        """
        row = await self.fetchrow(
            """
            INSERT INTO experts
                (name, display_name, description, icon, color, tags, expert_type,
                 config, prompts, owner_id, is_global, managed_key, seed_version)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb,
                    NULL, TRUE, $10, $11)
            ON CONFLICT (managed_key) WHERE managed_key IS NOT NULL DO NOTHING
            RETURNING *
            """,
            name,
            display_name,
            description,
            icon,
            color,
            tags or [],
            expert_type,
            json.dumps(config or {}),
            json.dumps(prompts or {}),
            managed_key,
            seed_version,
        )
        if row:
            return dict(row), True
        existing = await self.get_expert_by_managed_key(managed_key)
        if not existing:  # defensive: conflict target should make this impossible
            raise RuntimeError(f"managed expert seed disappeared: {managed_key}")
        return existing, False

    async def get_expert_visible_by_id(
        self,
        expert_id: str,
        *,
        user_id: str,
        project_ids: List[str] | None = None,
        is_admin: bool = False,
    ) -> Dict[str, Any] | None:
        """Return an expert only when the principal may select it."""
        proj = [UUID(str(p)) for p in (project_ids or [])]
        row = await self.fetchrow(
            """
            SELECT e.*
            FROM experts e
            WHERE e.id = $1
              AND (
                $4::boolean = TRUE
                OR e.owner_id = $2
                OR e.is_global = TRUE
                OR e.id IN (
                    SELECT expert_id FROM project_experts
                    WHERE project_id = ANY($3::uuid[])
                )
              )
            """,
            UUID(str(expert_id)),
            UUID(str(user_id)),
            proj,
            is_admin,
        )
        return dict(row) if row else None

    async def list_experts_visible(
        self,
        *,
        user_id: str,
        project_ids: List[str],
        expert_type: str | None = None,
    ) -> List[Dict[str, Any]]:
        """Owned + project-linked + global rows visible to the caller. Each row is
        annotated with the project_ids it is linked to (drives name precedence)."""
        proj = [UUID(str(p)) for p in project_ids] if project_ids else []
        rows = await self.fetch(
            """
            SELECT e.*,
                   COALESCE(
                     (SELECT array_agg(pe.project_id) FROM project_experts pe
                      WHERE pe.expert_id = e.id), '{}') AS project_ids
            FROM experts e
            WHERE ($3::text IS NULL OR e.expert_type = $3)
              AND (
                e.owner_id = $1
                OR e.is_global = TRUE
                OR e.id IN (SELECT expert_id FROM project_experts
                            WHERE project_id = ANY($2::uuid[]))
              )
            ORDER BY e.created_at DESC
            """,
            UUID(str(user_id)),
            proj,
            expert_type,
        )
        return [dict(r) for r in rows]

    # ── Default expert pointers ──────────────────────────────────────────

    async def ensure_application_expert_default(
        self, *, expert_type: str, expert_id: str
    ) -> Dict[str, Any]:
        """Seed an application pointer if the operator has not set one."""
        row = await self.fetchrow(
            """
            INSERT INTO application_expert_defaults (expert_type, expert_id)
            VALUES ($1, $2)
            ON CONFLICT (expert_type) DO NOTHING
            RETURNING *
            """,
            expert_type,
            UUID(str(expert_id)),
        )
        if row:
            await self.execute(
                """
                INSERT INTO expert_default_audit
                    (expert_type, scope_kind, new_expert_id, action)
                VALUES ($1, 'application', $2, 'seed')
                """,
                expert_type,
                UUID(str(expert_id)),
            )
            return dict(row)
        current = await self.get_application_expert_default(expert_type)
        if not current:
            raise RuntimeError(f"application default disappeared: {expert_type}")
        return current

    async def get_application_expert_default(
        self, expert_type: str
    ) -> Dict[str, Any] | None:
        row = await self.fetchrow(
            """
            SELECT d.expert_type AS default_type, d.created_at AS default_created_at,
                   d.updated_at AS default_updated_at, e.*
            FROM application_expert_defaults d
            JOIN experts e ON e.id = d.expert_id
            WHERE d.expert_type = $1
            """,
            expert_type,
        )
        return dict(row) if row else None

    async def list_application_expert_defaults(self) -> List[Dict[str, Any]]:
        rows = await self.fetch(
            """
            SELECT d.expert_type AS default_type, d.updated_at AS default_updated_at,
                   e.*
            FROM application_expert_defaults d
            JOIN experts e ON e.id = d.expert_id
            ORDER BY d.expert_type
            """
        )
        return [dict(r) for r in rows]

    async def set_application_expert_default(
        self, *, expert_type: str, expert_id: str, actor_user_id: str
    ) -> Dict[str, Any]:
        """Atomically validate and replace an operator-selected default."""
        eid = UUID(str(expert_id))
        actor = UUID(str(actor_user_id))
        async with self.acquire() as conn:
            async with conn.transaction():
                target = await conn.fetchrow(
                    "SELECT * FROM experts WHERE id = $1 FOR SHARE", eid
                )
                if not target:
                    raise ValueError("Expert not found")
                if target["expert_type"] != expert_type:
                    raise ValueError("Expert type does not match default slot")
                if not target["is_global"]:
                    raise ValueError("Application defaults must be global experts")
                previous = await conn.fetchval(
                    "SELECT expert_id FROM application_expert_defaults "
                    "WHERE expert_type = $1 FOR UPDATE",
                    expert_type,
                )
                row = await conn.fetchrow(
                    """
                    INSERT INTO application_expert_defaults
                        (expert_type, expert_id, updated_by)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (expert_type) DO UPDATE
                    SET expert_id = EXCLUDED.expert_id,
                        updated_by = EXCLUDED.updated_by,
                        updated_at = NOW()
                    RETURNING *
                    """,
                    expert_type,
                    eid,
                    actor,
                )
                await conn.execute(
                    """
                    INSERT INTO expert_default_audit
                        (actor_user_id, expert_type, scope_kind,
                         old_expert_id, new_expert_id, action)
                    VALUES ($1, $2, 'application', $3, $4, 'set')
                    """,
                    actor,
                    expert_type,
                    previous,
                    eid,
                )
        return dict(row)

    async def get_user_expert_default(
        self, *, user_id: str, expert_type: str
    ) -> Dict[str, Any] | None:
        row = await self.fetchrow(
            """
            SELECT d.user_id AS default_user_id,
                   d.expert_type AS default_type,
                   d.updated_at AS default_updated_at,
                   e.*
            FROM user_expert_defaults d
            JOIN experts e ON e.id = d.expert_id
            WHERE d.user_id = $1 AND d.expert_type = $2
            """,
            UUID(str(user_id)),
            expert_type,
        )
        return dict(row) if row else None

    async def list_user_expert_defaults(self, user_id: str) -> List[Dict[str, Any]]:
        rows = await self.fetch(
            """
            SELECT d.user_id AS default_user_id,
                   d.expert_type AS default_type,
                   d.updated_at AS default_updated_at,
                   e.*
            FROM user_expert_defaults d
            JOIN experts e ON e.id = d.expert_id
            WHERE d.user_id = $1
            ORDER BY d.expert_type
            """,
            UUID(str(user_id)),
        )
        return [dict(r) for r in rows]

    async def set_user_expert_default(
        self, *, user_id: str, expert_type: str, expert_id: str
    ) -> Dict[str, Any]:
        """Set a personal default, enforcing ownership and type atomically."""
        uid = UUID(str(user_id))
        eid = UUID(str(expert_id))
        async with self.acquire() as conn:
            async with conn.transaction():
                target = await conn.fetchrow(
                    "SELECT * FROM experts WHERE id = $1 FOR SHARE", eid
                )
                if not target:
                    raise ValueError("Expert not found")
                if target["expert_type"] != expert_type:
                    raise ValueError("Expert type does not match default slot")
                if target["owner_id"] != uid:
                    raise ValueError("Personal defaults must be owned by the user")
                previous = await conn.fetchval(
                    "SELECT expert_id FROM user_expert_defaults "
                    "WHERE user_id = $1 AND expert_type = $2 FOR UPDATE",
                    uid,
                    expert_type,
                )
                row = await conn.fetchrow(
                    """
                    INSERT INTO user_expert_defaults
                        (user_id, expert_type, expert_id)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (user_id, expert_type) DO UPDATE
                    SET expert_id = EXCLUDED.expert_id, updated_at = NOW()
                    RETURNING *
                    """,
                    uid,
                    expert_type,
                    eid,
                )
                await conn.execute(
                    """
                    INSERT INTO expert_default_audit
                        (actor_user_id, target_user_id, expert_type, scope_kind,
                         old_expert_id, new_expert_id, action)
                    VALUES ($1, $1, $2, 'user', $3, $4, 'set')
                    """,
                    uid,
                    expert_type,
                    previous,
                    eid,
                )
        return dict(row)

    async def clear_user_expert_default(
        self, *, user_id: str, expert_type: str
    ) -> bool:
        uid = UUID(str(user_id))
        async with self.acquire() as conn:
            async with conn.transaction():
                previous = await conn.fetchval(
                    "DELETE FROM user_expert_defaults "
                    "WHERE user_id = $1 AND expert_type = $2 RETURNING expert_id",
                    uid,
                    expert_type,
                )
                if previous is None:
                    return False
                await conn.execute(
                    """
                    INSERT INTO expert_default_audit
                        (actor_user_id, target_user_id, expert_type, scope_kind,
                         old_expert_id, action)
                    VALUES ($1, $1, $2, 'user', $3, 'clear')
                    """,
                    uid,
                    expert_type,
                    previous,
                )
        return True

    async def fork_and_set_user_expert_default(
        self,
        *,
        user_id: str,
        expert_type: str,
        source: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Atomically fork a source expert and make the fork the personal default."""
        uid = UUID(str(user_id))
        base_name = str(source["name"])
        config = source.get("config") or {}
        prompts = source.get("prompts") or {}
        if isinstance(config, str):
            config = json.loads(config)
        if isinstance(prompts, str):
            prompts = json.loads(prompts)
        async with self.acquire() as conn:
            async with conn.transaction():
                # Serialize fork naming for one principal; avoids a name race
                # without letting a unique violation poison the transaction.
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    str(uid),
                )
                name = ""
                for index in range(1, 100):
                    suffix = "default" if index == 1 else f"default-{index}"
                    candidate = f"{base_name}-{suffix}"[:100]
                    exists = await conn.fetchval(
                        "SELECT 1 FROM experts WHERE owner_id = $1 AND name = $2",
                        uid,
                        candidate,
                    )
                    if not exists:
                        name = candidate
                        break
                if not name:
                    raise ValueError("No free name for the personal default copy")
                expert = await conn.fetchrow(
                    """
                    INSERT INTO experts
                        (name, display_name, description, icon, color, tags,
                         expert_type, config, prompts, owner_id, is_global)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb,
                            $10, FALSE)
                    RETURNING *
                    """,
                    name,
                    f"{source['display_name']} (My Default)"[:200],
                    source.get("description"),
                    source.get("icon", "smart_toy"),
                    source.get("color", "#6B7280"),
                    source.get("tags") or [],
                    expert_type,
                    json.dumps(config),
                    json.dumps(prompts),
                    uid,
                )
                previous = await conn.fetchval(
                    "SELECT expert_id FROM user_expert_defaults "
                    "WHERE user_id = $1 AND expert_type = $2 FOR UPDATE",
                    uid,
                    expert_type,
                )
                await conn.execute(
                    """
                    INSERT INTO user_expert_defaults
                        (user_id, expert_type, expert_id)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (user_id, expert_type) DO UPDATE
                    SET expert_id = EXCLUDED.expert_id, updated_at = NOW()
                    """,
                    uid,
                    expert_type,
                    expert["id"],
                )
                await conn.execute(
                    """
                    INSERT INTO expert_default_audit
                        (actor_user_id, target_user_id, expert_type, scope_kind,
                         old_expert_id, new_expert_id, action)
                    VALUES ($1, $1, $2, 'user', $3, $4, 'set')
                    """,
                    uid,
                    expert_type,
                    previous,
                    expert["id"],
                )
        return dict(expert)

    async def get_project_default_expert(
        self, *, project_id: str, expert_type: str
    ) -> Dict[str, Any] | None:
        row = await self.fetchrow(
            """
            SELECT pe.project_id, pe.default_for, pe.config_override, e.*
            FROM project_experts pe
            JOIN experts e ON e.id = pe.expert_id
            WHERE pe.project_id = $1 AND pe.default_for = $2
              AND e.expert_type = $2
            """,
            UUID(str(project_id)),
            expert_type,
        )
        return dict(row) if row else None

    async def get_project_expert_link(
        self, *, project_id: str, expert_id: str
    ) -> Dict[str, Any] | None:
        row = await self.fetchrow(
            "SELECT * FROM project_experts WHERE project_id = $1 AND expert_id = $2",
            UUID(str(project_id)),
            UUID(str(expert_id)),
        )
        return dict(row) if row else None

    async def set_project_default_expert(
        self,
        *,
        project_id: str,
        expert_type: str,
        expert_id: str,
        actor_user_id: str,
        config_override: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        pid, eid = UUID(str(project_id)), UUID(str(expert_id))
        actor = UUID(str(actor_user_id))
        async with self.acquire() as conn:
            async with conn.transaction():
                target = await conn.fetchrow(
                    "SELECT expert_type FROM experts WHERE id = $1 FOR SHARE", eid
                )
                if not target:
                    raise ValueError("Expert not found")
                if target["expert_type"] != expert_type:
                    raise ValueError("Expert type does not match default slot")
                previous = await conn.fetchval(
                    "SELECT expert_id FROM project_experts "
                    "WHERE project_id = $1 AND default_for = $2 FOR UPDATE",
                    pid,
                    expert_type,
                )
                await conn.execute(
                    """
                    UPDATE project_experts SET default_for = NULL
                    WHERE project_id = $1 AND default_for = $2 AND expert_id <> $3
                    """,
                    pid,
                    expert_type,
                    eid,
                )
                row = await conn.fetchrow(
                    """
                    INSERT INTO project_experts
                        (project_id, expert_id, default_for, config_override)
                    VALUES ($1, $2, $3, $4::jsonb)
                    ON CONFLICT (project_id, expert_id) DO UPDATE
                    SET default_for = EXCLUDED.default_for,
                        config_override = COALESCE(
                            EXCLUDED.config_override, project_experts.config_override
                        )
                    RETURNING *
                    """,
                    pid,
                    eid,
                    expert_type,
                    json.dumps(config_override)
                    if config_override is not None
                    else None,
                )
                await conn.execute(
                    """
                    INSERT INTO expert_default_audit
                        (actor_user_id, target_project_id, expert_type, scope_kind,
                         old_expert_id, new_expert_id, action)
                    VALUES ($1, $2, $3, 'project', $4, $5, 'set')
                    """,
                    actor,
                    pid,
                    expert_type,
                    previous,
                    eid,
                )
        return dict(row)

    async def clear_project_default_expert(
        self, *, project_id: str, expert_type: str, actor_user_id: str
    ) -> bool:
        pid, actor = UUID(str(project_id)), UUID(str(actor_user_id))
        async with self.acquire() as conn:
            async with conn.transaction():
                previous = await conn.fetchval(
                    """
                    UPDATE project_experts SET default_for = NULL
                    WHERE project_id = $1 AND default_for = $2
                    RETURNING expert_id
                    """,
                    pid,
                    expert_type,
                )
                if previous is None:
                    return False
                await conn.execute(
                    """
                    INSERT INTO expert_default_audit
                        (actor_user_id, target_project_id, expert_type, scope_kind,
                         old_expert_id, action)
                    VALUES ($1, $2, $3, 'project', $4, 'clear')
                    """,
                    actor,
                    pid,
                    expert_type,
                    previous,
                )
        return True

    async def record_managed_expert_update(
        self, *, expert_id: str, expert_type: str, actor_user_id: str
    ) -> None:
        """Attribute operator edits to an insert-only platform seed row."""
        await self.execute(
            """
            INSERT INTO expert_default_audit
                (actor_user_id, expert_type, scope_kind,
                 old_expert_id, new_expert_id, action)
            VALUES ($1, $2, 'managed', $3, $3, 'update')
            """,
            UUID(str(actor_user_id)),
            expert_type,
            UUID(str(expert_id)),
        )

    async def update_expert(
        self, expert_id: str, *, updated_by: str, **fields: Any
    ) -> Dict[str, Any] | None:
        """Patch mutable fields (NOT expert_type — immutable, decision 3) and bump
        version. Column names come from a fixed allow-list, never user input."""
        allowed = {
            "display_name",
            "description",
            "icon",
            "color",
            "tags",
            "config",
            "prompts",
            "is_global",
        }
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            vals.append(json.dumps(v) if k in ("config", "prompts") else v)
            cast = "::jsonb" if k in ("config", "prompts") else ""
            sets.append(f"{k} = ${len(vals)}{cast}")
        if not sets:
            return await self.get_expert_by_id(expert_id)
        vals.append(UUID(str(updated_by)))
        vals.append(UUID(str(expert_id)))
        row = await self.fetchrow(
            f"""
            UPDATE experts
            SET {", ".join(sets)}, version = version + 1,
                updated_by = ${len(vals) - 1}, updated_at = NOW()
            WHERE id = ${len(vals)}
            RETURNING *
            """,
            *vals,
        )
        return dict(row) if row else None

    async def expert_delete_blockers(self, expert_id: str) -> List[Dict[str, Any]]:
        """Live references that block deletion (decision 15).

        Active threads, pending/unstarted jobs, pinned automations, and default
        pointers must be repointed first. Finished/running jobs never block
        because their resolved config is already frozen.
        """
        blockers: List[Dict[str, Any]] = []
        threads = await self.fetch(
            """
            SELECT id, title FROM threads
            WHERE status <> 'ended'
              AND metadata->>'expert_id' = $1
            """,
            str(expert_id),
        )
        blockers += [
            {"type": "thread", "id": str(t["id"]), "label": t["title"]} for t in threads
        ]
        # 'created'/'waiting' = truly unstarted (no resolved_config frozen yet) →
        # block. Running/review/paused jobs have a frozen config + ON DELETE SET
        # NULL covers history, so they never block (decision 15). NB: there is no
        # 'queued' status in this schema — the pending-for-agent state is 'waiting'.
        jobs = await self.fetch(
            "SELECT id, description FROM jobs "
            "WHERE expert_id = $1 AND status IN ('created', 'waiting')",
            UUID(str(expert_id)),
        )
        blockers += [
            {"type": "job", "id": str(j["id"]), "label": (j["description"] or "")[:80]}
            for j in jobs
        ]
        automations = await self.fetch(
            "SELECT id, name FROM automations WHERE expert_id = $1",
            UUID(str(expert_id)),
        )
        blockers += [
            {
                "type": "automation",
                "id": str(automation["id"]),
                "label": automation["name"],
            }
            for automation in automations
        ]
        app_defaults = await self.fetch(
            "SELECT expert_type FROM application_expert_defaults WHERE expert_id = $1",
            UUID(str(expert_id)),
        )
        blockers += [
            {
                "type": "application_default",
                "id": d["expert_type"],
                "label": f"Application {d['expert_type']} default",
            }
            for d in app_defaults
        ]
        user_defaults = await self.fetch(
            "SELECT user_id, expert_type FROM user_expert_defaults WHERE expert_id = $1",
            UUID(str(expert_id)),
        )
        blockers += [
            {
                "type": "user_default",
                "id": str(d["user_id"]),
                "label": f"User {d['expert_type']} default",
            }
            for d in user_defaults
        ]
        project_defaults = await self.fetch(
            "SELECT project_id, default_for FROM project_experts "
            "WHERE expert_id = $1 AND default_for IS NOT NULL",
            UUID(str(expert_id)),
        )
        blockers += [
            {
                "type": "project_default",
                "id": str(d["project_id"]),
                "label": f"Project {d['default_for']} default",
            }
            for d in project_defaults
        ]
        return blockers

    async def delete_expert(self, expert_id: str) -> bool:
        result = await self.execute(
            "DELETE FROM experts WHERE id = $1", UUID(str(expert_id))
        )
        return result == "DELETE 1"

    # ── Skills (Agent Skills, Slice 1: authoring foundation) ──────────────
    async def create_skill(
        self,
        *,
        name: str,
        display_name: str,
        owner_id: str,
        files: Dict[str, str],
        description: str | None = None,
        icon: str = "extension",
        color: str = "#6B7280",
        tags: List[str] | None = None,
        is_global: bool = False,
    ) -> Dict[str, Any]:
        """Insert an owned skill + its files atomically. (name, owner_id) unique."""
        async with self.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO skills
                        (name, display_name, description, icon, color, tags,
                         owner_id, is_global)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    RETURNING *
                    """,
                    name,
                    display_name,
                    description,
                    icon,
                    color,
                    tags or [],
                    UUID(str(owner_id)),
                    is_global,
                )
                await conn.executemany(
                    "INSERT INTO skill_files (skill_id, path, content) "
                    "VALUES ($1, $2, $3)",
                    [(row["id"], p, c) for p, c in sorted(files.items())],
                )
        return dict(row)

    async def get_skill_by_id(self, skill_id: str) -> Dict[str, Any] | None:
        row = await self.fetchrow(
            "SELECT * FROM skills WHERE id = $1", UUID(str(skill_id))
        )
        return dict(row) if row else None

    async def get_skill_files(self, skill_id: str) -> Dict[str, str]:
        rows = await self.fetch(
            "SELECT path, content FROM skill_files WHERE skill_id = $1 ORDER BY path",
            UUID(str(skill_id)),
        )
        return {r["path"]: r["content"] for r in rows}

    async def list_skills_visible(self, *, user_id: str) -> List[Dict[str, Any]]:
        """Owned + global skills visible to the caller. No project junction in
        Slice 1 (project skills come from the Gitea repo at runtime — Slice 2)."""
        rows = await self.fetch(
            """
            SELECT * FROM skills
            WHERE owner_id = $1 OR is_global = TRUE
            ORDER BY created_at DESC
            """,
            UUID(str(user_id)),
        )
        return [dict(r) for r in rows]

    async def update_skill(
        self,
        skill_id: str,
        *,
        updated_by: str,
        files: Dict[str, str] | None = None,
        **fields: Any,
    ) -> Dict[str, Any] | None:
        """Patch mutable metadata (NOT name — immutable) + optionally replace the
        file set, bumping version. Column names come from a fixed allow-list."""
        allowed = {"display_name", "description", "icon", "color", "tags", "is_global"}
        async with self.acquire() as conn:
            async with conn.transaction():
                sets, vals = [], []
                for k, v in fields.items():
                    if k not in allowed:
                        continue
                    vals.append(v)
                    sets.append(f"{k} = ${len(vals)}")
                set_sql = (", ".join(sets) + ", ") if sets else ""
                vals.append(UUID(str(updated_by)))
                vals.append(UUID(str(skill_id)))
                row = await conn.fetchrow(
                    f"""
                    UPDATE skills
                    SET {set_sql}version = version + 1,
                        updated_by = ${len(vals) - 1}, updated_at = NOW()
                    WHERE id = ${len(vals)}
                    RETURNING *
                    """,
                    *vals,
                )
                if row and files is not None:
                    await conn.execute(
                        "DELETE FROM skill_files WHERE skill_id = $1",
                        UUID(str(skill_id)),
                    )
                    await conn.executemany(
                        "INSERT INTO skill_files (skill_id, path, content) "
                        "VALUES ($1, $2, $3)",
                        [(row["id"], p, c) for p, c in sorted(files.items())],
                    )
        return dict(row) if row else None

    async def delete_skill(self, skill_id: str) -> bool:
        result = await self.execute(
            "DELETE FROM skills WHERE id = $1", UUID(str(skill_id))
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
    # Reads feed every model picker (session/job) and the resolver's
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
        as themselves — only literal ``None`` is treated as "use default".

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
    # Keys: llm.default_browser_model / llm.default_citation_model
    # Value shape: {"model": "<model_id>"}
    # Replaces the BROWSER_LLM_MODEL / CITATION_LLM_MODEL
    # env vars as the source of truth for the default model per workload.
    # =========================================================================

    @staticmethod
    def _default_llm_model_key(kind: str) -> str:
        return f"llm.default_{kind}_model"

    async def get_default_llm_model(self, kind: str) -> str | None:
        """Return the configured default model ID for ``kind`` or None.

        ``kind`` is one of 'browser', 'citation'.
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

    async def get_user_cloud_identity(self, user_id: str) -> Dict[str, Any]:
        """Get a user's per-backend cloud identity cache. Empty dict if unset.

        Shape: ``{"<backend_id>": {"user_id", "home_browser_url", "resolved_at"}}``.
        Maintained by services/cloud/identity.py — positive resolutions only.
        """
        try:
            uuid_val = UUID(user_id)
        except (ValueError, TypeError):
            return {}
        async with self.acquire() as conn:
            val = await conn.fetchval(
                "SELECT COALESCE(cloud_identity, '{}') FROM users WHERE id = $1",
                uuid_val,
            )
            if val is None:
                return {}
            return json.loads(val) if isinstance(val, str) else val

    async def merge_user_cloud_identity(
        self, user_id: str, backend_id: str, entry: Dict[str, Any]
    ) -> bool:
        """Merge fields into one backend's entry in users.cloud_identity.

        Single-statement ``jsonb_set(..., existing || $3)`` so concurrent
        resolvers (e.g. member sync fan-out) can't lose each other's fields.
        """
        try:
            uuid_val = UUID(user_id)
        except (ValueError, TypeError):
            return False
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE users
                SET cloud_identity = jsonb_set(
                    COALESCE(cloud_identity, '{}'::jsonb),
                    ARRAY[$2],
                    COALESCE(cloud_identity -> $2, '{}'::jsonb) || $3::jsonb
                )
                WHERE id = $1
                """,
                uuid_val,
                backend_id,
                json.dumps(entry),
            )
            return result == "UPDATE 1"

    async def list_active_ide_workspaces(self) -> List[Dict[str, Any]]:
        """Return active IDE-enabled workspaces (jobs + threads).

        Used by the code-server settings sweeper to know which workspaces to pull
        config from. A workspace counts as active when it has a ready container,
        a ready VM, or an active/idle restored IDE session. Each row is
        ``{"entity_type", "id", "user_id", "context"}``; ``context`` is the raw
        JSONB (jobs.context / threads.metadata) and may be a JSON string.
        """
        out: List[Dict[str, Any]] = []
        async with self.acquire() as conn:
            job_rows = await conn.fetch(
                """
                SELECT id, user_id, context
                FROM jobs
                WHERE context->'ide_session'->>'status' IN ('active', 'idle')
                   OR context->'workspace_container'->>'status' = 'ready'
                   OR context->'vm'->>'status' = 'ready'
                """
            )
            for r in job_rows:
                out.append(
                    {
                        "entity_type": "job",
                        "id": str(r["id"]),
                        "user_id": str(r["user_id"]) if r["user_id"] else None,
                        "context": r["context"],
                    }
                )
            thread_rows = await conn.fetch(
                """
                SELECT id, user_id, metadata
                FROM threads
                WHERE metadata->'ide_session'->>'status' IN ('active', 'idle')
                   OR metadata->'workspace_container'->>'status' = 'ready'
                   OR metadata->'vm'->>'status' = 'ready'
                """
            )
            for r in thread_rows:
                out.append(
                    {
                        "entity_type": "thread",
                        "id": str(r["id"]),
                        "user_id": str(r["user_id"]) if r["user_id"] else None,
                        "context": r["metadata"],
                    }
                )
        return out

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
                       is_admin, can_use_vm, is_approved, approved_at, approved_by,
                       created_at
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
                       is_admin, can_use_vm, is_approved, preferred_username,
                       keycloak_sub, created_at
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
                       is_admin, can_use_vm, is_approved, preferred_username,
                       keycloak_sub, created_at
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
        is_approved: bool = False,
        preferred_username: str | None = None,
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
            is_approved: Effective approval at JIT time (role_approved). Never
                downgrades an existing row — merged with OR.
            preferred_username: Keycloak preferred_username, persisted so
                approval-time provisioning can rebuild the ensure-user payload.

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
                    updated = await conn.fetchrow(
                        """
                        UPDATE users
                        SET keycloak_sub = $1,
                            is_admin = $2,
                            is_approved = (is_approved OR $3),
                            preferred_username = COALESCE($4, preferred_username)
                        WHERE id = $5
                        RETURNING is_approved, preferred_username
                        """,
                        sub,
                        is_admin,
                        is_approved,
                        preferred_username,
                        existing["id"],
                    )
                    result = dict(existing)
                    result["keycloak_sub"] = sub
                    result["is_admin"] = is_admin
                    result["is_approved"] = updated["is_approved"]
                    result["preferred_username"] = updated["preferred_username"]
                    return result

            # Check if keycloak_sub already linked (concurrent request)
            existing_sub = await conn.fetchrow(
                """
                SELECT id, display_name, avatar_color, email, default_project_id,
                       is_admin, can_use_vm, is_approved, preferred_username,
                       keycloak_sub, created_at
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
                                          is_approved, approved_at, preferred_username,
                                          keycloak_sub, default_project_id)
                        VALUES ($1, '#89b4fa', $2, $3, $4,
                                CASE WHEN $4 THEN NOW() ELSE NULL END, $5, $6,
                                (SELECT id FROM new_project))
                        RETURNING id, display_name, avatar_color, email, default_project_id,
                                  is_admin, can_use_vm, is_approved, preferred_username,
                                  keycloak_sub, created_at
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
                    is_approved,
                    preferred_username,
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
                               is_admin, can_use_vm, is_approved, preferred_username,
                               keycloak_sub, created_at
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
        is_approved: bool | None = None,
        approved_at: datetime | None = None,
        approved_by: str | None = None,
    ) -> bool:
        """Update a user.

        Args:
            user_id: User UUID
            display_name: New display name
            avatar_color: New avatar color
            email: New email address
            is_admin: New admin flag (admin-only callers)
            can_use_vm: New per-user VM grant (admin-only callers)
            is_approved: New admission flag (admin-only callers)
            approved_at: Approval timestamp to stamp (pair with is_approved=True;
                omit on suspension so the original approval time survives)
            approved_by: Admin UUID who approved (pair with is_approved=True)

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

        if is_approved is not None:
            param_count += 1
            updates.append(f"is_approved = ${param_count}")
            values.append(bool(is_approved))

        if approved_at is not None:
            param_count += 1
            updates.append(f"approved_at = ${param_count}")
            values.append(approved_at)

        if approved_by is not None:
            param_count += 1
            updates.append(f"approved_by = ${param_count}")
            values.append(
                UUID(approved_by) if isinstance(approved_by, str) else approved_by
            )

        if not updates:
            return False

        param_count += 1
        values.append(uuid_val)

        query = f"UPDATE users SET {', '.join(updates)} WHERE id = ${param_count}"

        async with self.acquire() as conn:
            result = await conn.execute(query, *values)

        return result == "UPDATE 1"

    async def approve_users(
        self, user_ids: list[str], approved_by: str | None = None
    ) -> list[str]:
        """Bulk-approve users in a single transaction (admin-only callers).

        Stamps ``is_approved=TRUE, approved_at=NOW(), approved_by=<admin>`` for
        every id that resolves to an existing row, and returns the subset of
        ids that matched. The caller maps any requested id missing from the
        result to "not found". Idempotent — re-approving just re-stamps.

        Args:
            user_ids: User UUIDs to approve.
            approved_by: Admin UUID performing the approval (NULL = system).

        Returns:
            List of matched user-id strings.
        """
        uuids: list[UUID] = []
        for uid in user_ids:
            try:
                uuids.append(UUID(uid))
            except (ValueError, TypeError, AttributeError):
                continue
        if not uuids:
            return []
        approver: UUID | None = None
        if approved_by:
            try:
                approver = UUID(approved_by)
            except (ValueError, TypeError, AttributeError):
                approver = None

        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE users
                SET is_approved = TRUE, approved_at = NOW(), approved_by = $2
                WHERE id = ANY($1::uuid[])
                RETURNING id
                """,
                uuids,
                approver,
            )
        return [str(row["id"]) for row in rows]

    # ------------------------------------------------------------------
    # Security events (denied-access audit) — security_event_log.md
    # ------------------------------------------------------------------

    async def record_security_event(
        self,
        *,
        event_type: str,
        resource_type: str,
        user_id: str | None = None,
        auth_method: str | None = None,
        real_is_admin: bool = False,
        view_as: bool = False,
        resource_id: str | None = None,
        method: str | None = None,
        path: str | None = None,
        detail: str | None = None,
        client_ip: str | None = None,
    ) -> None:
        """Insert one denied-access audit row.

        Callers (``security.access.log_security_event``) wrap this in
        try/except — a failed insert must never block the 403 it
        documents. A malformed ``user_id`` degrades to NULL rather than
        failing: the row with method/path/ip is still worth keeping.
        """
        uid: UUID | None = None
        if user_id:
            try:
                uid = UUID(str(user_id))
            except (ValueError, TypeError, AttributeError):
                uid = None
        async with self.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO security_events
                    (event_type, user_id, auth_method, real_is_admin, view_as,
                     resource_type, resource_id, method, path, detail, client_ip)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                event_type,
                uid,
                auth_method,
                real_is_admin,
                view_as,
                resource_type,
                str(resource_id) if resource_id is not None else None,
                method,
                path,
                detail,
                client_ip,
            )

    async def list_security_events(
        self,
        *,
        limit: int = 100,
        user_id: str | None = None,
        event_type: str | None = None,
        since: datetime | None = None,
    ) -> List[Dict[str, Any]]:
        """List denied-access events, newest first, with optional filters.

        Args:
            limit: Maximum rows (clamped to 1000).
            user_id: Only events for this caller UUID.
            event_type: Only this type (``access_denied`` / ``admin_denied``).
            since: Only events at or after this timestamp.

        Returns:
            List of event dicts ordered by created_at DESC.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if user_id:
            try:
                params.append(UUID(str(user_id)))
                clauses.append(f"user_id = ${len(params)}")
            except (ValueError, TypeError, AttributeError):
                # Unparseable filter matches nothing rather than everything.
                clauses.append("FALSE")
        if event_type:
            params.append(event_type)
            clauses.append(f"event_type = ${len(params)}")
        if since:
            params.append(since)
            clauses.append(f"created_at >= ${len(params)}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 1000)))
        async with self.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, created_at, event_type, user_id, auth_method,
                       real_is_admin, view_as, resource_type, resource_id,
                       method, path, detail, client_ip
                FROM security_events
                {where}
                ORDER BY created_at DESC
                LIMIT ${len(params)}
                """,
                *params,
            )
        return [dict(row) for row in rows]

    async def prune_security_events(self, retention_days: int = 90) -> int:
        """Delete events older than ``retention_days``. Returns rows deleted."""
        async with self.acquire() as conn:
            count = await conn.fetchval(
                """
                WITH deleted AS (
                    DELETE FROM security_events
                    WHERE created_at < NOW() - make_interval(days => $1)
                    RETURNING 1
                ) SELECT COUNT(*) FROM deleted
                """,
                int(retention_days),
            )
        return int(count or 0)

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
            async with conn.transaction():
                # No FK cascade fires on capability_grants.scope_id (polymorphic);
                # delete the user's grant rows in-band so removal stays atomic.
                await self.delete_grants_for_scope(
                    conn, scope_kind="user", scope_id=user_id
                )
                result = await conn.execute(
                    "DELETE FROM users WHERE id = $1",
                    uuid_val,
                )

        return result == "DELETE 1"

    async def get_admin_user(self) -> Dict[str, Any] | None:
        """Get the first admin user."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE is_admin = TRUE LIMIT 1"
            )
            return dict(row) if row else None

    async def list_admin_user_ids(self) -> list[str]:
        """Return ids of all admin users (fan-out target for notifications)."""
        async with self.acquire() as conn:
            rows = await conn.fetch("SELECT id FROM users WHERE is_admin = TRUE")
        return [str(row["id"]) for row in rows]

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
                       network_tier,
                       (SELECT COUNT(*) FROM jobs j WHERE j.project_id = projects.id) AS job_count,
                       created_at, updated_at
                FROM projects
                WHERE id = $1
                """,
                uuid_val,
            )

        return dict(row) if row else None

    async def get_workspace_network_tier(
        self, work_id: str, kind: str
    ) -> Optional[str]:
        """Resolve the network_tier of the project that owns a job/thread.

        Returns the bare tier string (e.g. ``'internet-only'``,
        ``'home-allowed'``) or ``None`` if no project mapping exists.
        The caller is responsible for applying its own default when the
        result is ``None`` — keeping the DB layer free of policy.

        Args:
            work_id: Job or thread UUID as string.
            kind: ``'job'`` or ``'thread'``.
        """
        try:
            uuid_val = UUID(work_id)
        except ValueError:
            return None

        if kind == "job":
            query = (
                "SELECT p.network_tier "
                "FROM projects p JOIN jobs j ON j.project_id = p.id "
                "WHERE j.id = $1"
            )
        elif kind == "thread":
            query = (
                "SELECT p.network_tier "
                "FROM projects p JOIN threads t ON t.project_id = p.id "
                "WHERE t.id = $1"
            )
        else:
            return None

        async with self.acquire() as conn:
            row = await conn.fetchrow(query, uuid_val)

        return row["network_tier"] if row else None

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
            # Admin-only at the API layer (orchestrator/main.py update_project);
            # listed here so the DB UPDATE path accepts it once authorized.
            "network_tier",
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
            async with conn.transaction():
                # No FK cascade fires on capability_grants.scope_id (polymorphic);
                # delete the project's grant rows in-band so removal stays atomic.
                await self.delete_grants_for_scope(
                    conn, scope_kind="project", scope_id=project_id
                )
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

        Used by the IMAP poller as a fast-path dedup check. NOT race-safe on
        its own (read-then-act) — the authoritative guard is
        ``claim_inbound_email`` below.
        """
        async with self.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM message_log WHERE email_message_id = $1",
                email_message_id,
            )
        return (count or 0) > 0

    async def claim_inbound_email(self, email_message_id: str) -> bool:
        """Atomically claim an inbound email for routing (M1 — HA dedup).

        Inserts the RFC822 Message-ID into ``processed_inbound_emails``;
        returns True iff THIS call inserted it (i.e. won the claim). The imap
        poller is leader-gated, but leader election has no fencing — during a
        partition / Postgres failover two replicas can briefly both poll IMAP.
        Claiming before routing means a concurrent poller in that window loses
        the insert and skips, so an inbound reply is never injected into a job
        twice. See migration 0037_processed_inbound_emails.sql.
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO processed_inbound_emails (email_message_id)
                VALUES ($1)
                ON CONFLICT (email_message_id) DO NOTHING
                RETURNING email_message_id
                """,
                email_message_id,
            )
        return row is not None

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

    async def get_pending_action_counts(
        self,
        owner_user_id: Optional[str] = None,
        visible_project_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Get counts of pending actions across all types.

        Visibility (P4e): when ``owner_user_id`` is provided, counts and the
        ``most_urgent`` sudo are restricted to jobs the caller can see
        (their own jobs OR jobs in projects they're a member of). Pass
        ``None`` for the admin view (counts across all jobs). Jobs with no
        owner are admin-only — they don't appear in any user's view.

        Returns:
            Dict with sudo, messages, reviews counts and most_urgent info.
        """
        admin_view = owner_user_id is None
        project_ids = visible_project_ids or []

        async with self.acquire() as conn:
            if admin_view:
                sudo_count = (
                    await conn.fetchval(
                        "SELECT COUNT(*) FROM sudo_approval_requests "
                        "WHERE status = 'pending'"
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
                most_urgent_sudo = await conn.fetchrow(
                    """
                    SELECT id, command, expires_at
                    FROM sudo_approval_requests
                    WHERE status = 'pending' AND expires_at > NOW()
                    ORDER BY expires_at ASC
                    LIMIT 1
                    """
                )
            else:
                # Visibility OR-clause: caller's own jobs ∪ project jobs.
                # Empty project_ids → ANY($2::uuid[]) on an empty array
                # yields false, so we still return own-jobs-only.
                sudo_count = (
                    await conn.fetchval(
                        """SELECT COUNT(*) FROM sudo_approval_requests s
                        JOIN jobs j ON s.job_id = j.id
                        WHERE s.status = 'pending'
                          AND (j.user_id = $1 OR j.project_id = ANY($2::uuid[]))""",
                        owner_user_id,
                        project_ids,
                    )
                    or 0
                )
                message_count = (
                    await conn.fetchval(
                        """SELECT COUNT(*) FROM jobs
                        WHERE status = 'waiting_for_reply'
                          AND (user_id = $1 OR project_id = ANY($2::uuid[]))""",
                        owner_user_id,
                        project_ids,
                    )
                    or 0
                )
                review_count = (
                    await conn.fetchval(
                        """SELECT COUNT(*) FROM jobs
                        WHERE status = 'pending_review'
                          AND (user_id = $1 OR project_id = ANY($2::uuid[]))""",
                        owner_user_id,
                        project_ids,
                    )
                    or 0
                )
                most_urgent_sudo = await conn.fetchrow(
                    """SELECT s.id, s.command, s.expires_at
                    FROM sudo_approval_requests s
                    JOIN jobs j ON s.job_id = j.id
                    WHERE s.status = 'pending' AND s.expires_at > NOW()
                      AND (j.user_id = $1 OR j.project_id = ANY($2::uuid[]))
                    ORDER BY s.expires_at ASC
                    LIMIT 1""",
                    owner_user_id,
                    project_ids,
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

    async def claim_pending_notifications(self, user_id: str) -> List[Dict[str, Any]]:
        """Atomically claim a user's undelivered notifications for a digest.

        Stamps ``delivered_at = NOW()`` and RETURNs only the rows THIS call
        flipped (``delivered_at`` was NULL). Two quiet-hours digest loops in
        the transient dual-leader window therefore split the pending set
        disjointly — in practice one claims all and the other gets none — so a
        digest email is never sent twice. If dispatch then fails, release the
        claim with ``unmark_notifications_delivered`` so it retries next cycle.
        """
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE notification_queue
                   SET delivered_at = NOW()
                 WHERE user_id = $1 AND delivered_at IS NULL
                RETURNING id, user_id, job_id, thread_id, subject, message,
                          channels, queued_at
                """,
                UUID(user_id),
            )
        return [dict(r) for r in rows]

    async def unmark_notifications_delivered(self, ids: List[str]) -> int:
        """Release a digest claim (``delivered_at`` → NULL) so the
        notifications are retried next cycle. Used when dispatch fails after
        ``claim_pending_notifications`` won them. Returns count updated."""
        if not ids:
            return 0
        uuids = [UUID(i) for i in ids]
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE notification_queue
                SET delivered_at = NULL
                WHERE id = ANY($1::uuid[])
                """,
                uuids,
            )
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
    # --- Capability grants (Slice 2) ---

    async def list_grants_for_scopes(
        self, *, user_id: str | None, project_ids: list[str]
    ) -> dict[str, list[dict]]:
        """{'user': [...], 'project': [...], 'global': [...]} of {key, value_json}.
        JSONB is returned by asyncpg as a STRING here — deserialize on read."""
        rows = await self.fetch(
            """
            SELECT scope_kind, key, value_json FROM capability_grants
            WHERE (scope_kind = 'global')
               OR (scope_kind = 'user'    AND scope_id = $1)
               OR (scope_kind = 'project' AND scope_id = ANY($2::uuid[]))
            """,
            UUID(user_id) if user_id else None,
            [UUID(p) for p in project_ids],
        )
        out: dict[str, list[dict]] = {"user": [], "project": [], "global": []}
        for r in rows:
            raw = r["value_json"]
            val = json.loads(raw) if isinstance(raw, str) else raw
            out[r["scope_kind"]].append({"key": r["key"], "value_json": val})
        return out

    async def list_grants(self, *, scope_kind: str, scope_id: str | None) -> list[dict]:
        rows = await self.fetch(
            "SELECT key, value_json, granted_by, updated_at FROM capability_grants "
            "WHERE scope_kind = $1 AND scope_id IS NOT DISTINCT FROM $2 ORDER BY key",
            scope_kind,
            UUID(scope_id) if scope_id else None,
        )
        result = []
        for r in rows:
            d = dict(r)
            if isinstance(d.get("value_json"), str):
                d["value_json"] = json.loads(d["value_json"])
            result.append(d)
        return result

    async def set_grant(
        self,
        *,
        scope_kind: str,
        scope_id: str | None,
        key: str,
        value_json: Any,
        actor: str | None,
    ) -> dict:
        """Upsert one grant row (idempotent); granted_by records the acting admin."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO capability_grants (scope_kind, scope_id, key, value_json, granted_by)
                VALUES ($1, $2, $3, $4::jsonb, $5)
                ON CONFLICT (scope_kind, scope_id, key) DO UPDATE
                    SET value_json = EXCLUDED.value_json, granted_by = EXCLUDED.granted_by,
                        updated_at = NOW()
                RETURNING *
                """,
                scope_kind,
                UUID(scope_id) if scope_id else None,
                key,
                json.dumps(value_json),
                UUID(actor) if actor else None,
            )
            d = dict(row)
            if isinstance(d.get("value_json"), str):
                d["value_json"] = json.loads(d["value_json"])
            return d

    async def delete_grant(
        self,
        *,
        scope_kind: str,
        scope_id: str | None,
        key: str,
    ) -> bool:
        async with self.acquire() as conn:
            prev = await conn.fetchrow(
                "DELETE FROM capability_grants WHERE scope_kind=$1 "
                "AND scope_id IS NOT DISTINCT FROM $2 AND key=$3 RETURNING value_json",
                scope_kind,
                UUID(scope_id) if scope_id else None,
                key,
            )
            return prev is not None

    async def delete_grants_for_scope(
        self, conn, *, scope_kind: str, scope_id: str
    ) -> int:
        """Hard-delete a removed user/project's grant rows (no FK cascade fires —
        decision 23). Takes an existing connection so it runs in the caller's
        delete transaction (atomic with the principal removal)."""
        result = await conn.execute(
            "DELETE FROM capability_grants WHERE scope_kind=$1 AND scope_id=$2",
            scope_kind,
            UUID(scope_id),
        )
        return int(result.split()[-1]) if result else 0

    async def user_can_use_vm(self, user: dict) -> bool:
        """Effective vm_workspace grant; fall back to the legacy can_use_vm column
        during rollout / on grant-read failure (per-capability fail-mode).

        Admins short-circuit to True — grants restrict non-admins only (the PDP
        takes ``is_admin`` the same way). Without this, a direct caller that
        doesn't pre-check ``is_admin`` (unlike ``_check_vm_permission``) would
        misclassify an admin with zero grant rows as un-entitled."""
        if user.get("is_admin"):
            return True
        try:
            scoped = await self.list_grants_for_scopes(
                user_id=str(user["id"]), project_ids=[]
            )
            from src.core.capability_grants import resolve_grants

            g = resolve_grants(
                user_rows=scoped["user"],
                project_rows=scoped["project"],
                global_rows=scoped["global"],
            )
            if any(
                r["key"] == "vm_workspace"
                for r in scoped["user"] + scoped["project"] + scoped["global"]
            ):
                return bool(g["vm_workspace"])
        except Exception:
            logger.exception("vm grant read failed; fall back to can_use_vm column")
        return bool(user.get("can_use_vm"))

    async def user_can_publish_datasource(self, user: dict) -> bool:
        """Effective public_datasources grant (publish is_global datasources).

        Admins short-circuit to True. Unlike user_can_use_vm there is no
        legacy-column fallback (new capability, deny-by-default) and the
        fail mode is CLOSED: publishing hands the publisher's credentials
        to every user's agents, so a grant-read failure must deny.
        Spec: docs/features/public_datasources.md.
        """
        if user.get("is_admin"):
            return True
        try:
            scoped = await self.list_grants_for_scopes(
                user_id=str(user["id"]), project_ids=[]
            )
            from src.core.capability_grants import resolve_grants

            g = resolve_grants(
                user_rows=scoped["user"],
                project_rows=scoped["project"],
                global_rows=scoped["global"],
            )
            return bool(g.get("public_datasources"))
        except Exception:
            logger.exception("public_datasources grant read failed; denying publish")
            return False

    async def user_can_autonomous_send(self, user: dict) -> bool:
        """Effective email_autonomous_send grant (unattended email send).

        Admins short-circuit to True. Mirrors user_can_publish_datasource:
        no legacy-column fallback (new capability, deny-by-default) and the
        fail mode is CLOSED — unattended send bypasses the human
        send-approval freeze, so a grant-read failure must deny.
        Spec: docs/features/email_datasource.md.
        """
        if user.get("is_admin"):
            return True
        try:
            scoped = await self.list_grants_for_scopes(
                user_id=str(user["id"]), project_ids=[]
            )
            from src.core.capability_grants import resolve_grants

            g = resolve_grants(
                user_rows=scoped["user"],
                project_rows=scoped["project"],
                global_rows=scoped["global"],
            )
            return bool(g.get("email_autonomous_send"))
        except Exception:
            logger.exception(
                "email_autonomous_send grant read failed; denying unattended send"
            )
            return False

    # Max-level user-scope grants for admins. Admins bypass the PDP at every
    # PEP, so these rows change no enforcement outcome — they make the DATA
    # tell the truth (Grants UI, and any future admin-agnostic caller) instead
    # of an empty grant list that reads as "no permission". Kept in lockstep
    # with migration 0049_backfill_admin_grants.sql. NOTE: on admin DEMOTION
    # these become live grants for the ex-admin — review them then.
    _ADMIN_GRANT_SEED: tuple[tuple[str, str], ...] = (
        ("vm_workspace", "true"),
        ("shell_tools", "true"),
        ("delegation", "true"),
        ("autonomy_ceiling", '"full"'),
        ("permission_mode", '"autonomous"'),
    )

    async def seed_admin_grants(self, user_id: str) -> None:
        """Idempotently seed max-level user-scope grants for an admin user.

        Called on first admin login and on promotion to admin (auth.py);
        existing admins are backfilled by migration 0049. Best-effort — a
        failure never blocks authentication.
        """
        try:
            async with self.acquire() as conn:
                for key, value_json in self._ADMIN_GRANT_SEED:
                    await conn.execute(
                        """
                        INSERT INTO capability_grants
                            (scope_kind, scope_id, key, value_json, granted_by)
                        VALUES ('user', $1, $2, $3::jsonb, NULL)
                        ON CONFLICT (scope_kind, scope_id, key) DO NOTHING
                        """,
                        UUID(str(user_id)),
                        key,
                        value_json,
                    )
        except Exception:
            logger.exception("Failed to seed admin grants for %s", user_id)

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
        # ``updated_by`` is a TEXT column; coerce non-str actor ids (e.g. a
        # UUID) so callers passing a raw uuid don't trip asyncpg's type check.
        if updated_by is not None:
            updated_by = str(updated_by)
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
    # AUTOMATIONS (cron + event-trigger job templates; see
    # docs/features/automations_v0.md and migration 0015_create_automations.sql)
    # =========================================================================

    async def create_automation(
        self,
        *,
        owner_id: str,
        name: str,
        trigger_type: str,
        expert: str,
        prompt: str,
        expert_id: str | None = None,
        description: str | None = None,
        project_id: str | None = None,
        cron_expr: str | None = None,
        timezone: str = "UTC",
        catchup_window_seconds: int = 86400,
        event_filter: Dict[str, Any] | None = None,
        enabled: bool = True,
        config_override: Dict[str, Any] | None = None,
        autonomy: str = "review",
        priority: int = 5,
        max_chain_depth: int = 10,
        max_fires_per_day: int = 100,
        next_run_at: datetime | None = None,
    ) -> Dict[str, Any]:
        """Insert an automation row. Caller pre-computes ``next_run_at`` for
        cron triggers (via croniter against the row's timezone) so the
        dispatcher's first tick can pick it up without a recompute pass.

        v0 only stores cron rows from the API; ``event_filter`` is accepted
        here so the v0.5 event dispatcher can land without a schema change.
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO automations (
                    owner_id, project_id, name, description, trigger_type,
                    cron_expr, timezone, catchup_window_seconds,
                    event_filter, enabled,
                    expert, expert_id, prompt, config_override, autonomy, priority,
                    max_chain_depth, max_fires_per_day,
                    next_run_at
                )
                VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8,
                    $9::jsonb, $10,
                    $11, $12, $13, $14::jsonb, $15, $16,
                    $17, $18,
                    $19
                )
                RETURNING *
                """,
                UUID(owner_id),
                UUID(project_id) if project_id else None,
                name,
                description,
                trigger_type,
                cron_expr,
                timezone,
                catchup_window_seconds,
                json.dumps(event_filter) if event_filter else None,
                enabled,
                expert,
                UUID(expert_id) if expert_id else None,
                prompt,
                json.dumps(config_override or {}),
                autonomy,
                priority,
                max_chain_depth,
                max_fires_per_day,
                next_run_at,
            )
        return self._automation_row_to_dict(row)

    async def get_automation(self, automation_id: str) -> Dict[str, Any] | None:
        """Fetch one automation by id. Returns None if not found."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM automations WHERE id = $1",
                UUID(automation_id),
            )
        return self._automation_row_to_dict(row) if row else None

    async def list_automations(
        self,
        *,
        owner_id: str | None = None,
        project_id: str | None = None,
    ) -> List[Dict[str, Any]]:
        """List automations, newest first.

        ``owner_id`` and ``project_id`` are both optional and additive:
        passing both narrows to a single user's automations within one
        project. Admin callers can pass neither to see everything.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if owner_id is not None:
            params.append(UUID(owner_id))
            clauses.append(f"owner_id = ${len(params)}")
        if project_id is not None:
            params.append(UUID(project_id))
            clauses.append(f"project_id = ${len(params)}")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        async with self.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM automations {where} ORDER BY created_at DESC",
                *params,
            )
        return [self._automation_row_to_dict(r) for r in rows]

    # Fields the editor is allowed to mutate. Ownership, trigger_type,
    # event_filter, and all *_at / run_count / fires_today_* state are
    # locked. Renaming an automation must not require resending its prompt.
    _AUTOMATION_UPDATABLE_FIELDS = frozenset(
        {
            "name",
            "description",
            "cron_expr",
            "timezone",
            "catchup_window_seconds",
            "enabled",
            "expert",
            "expert_id",
            "prompt",
            "config_override",
            "autonomy",
            "priority",
            "max_chain_depth",
            "max_fires_per_day",
            "next_run_at",
        }
    )

    async def update_automation(
        self,
        automation_id: str,
        **fields: Any,
    ) -> Dict[str, Any] | None:
        """Partial update of an automation row.

        Caller is responsible for recomputing ``next_run_at`` when changing
        ``cron_expr`` / ``timezone`` / ``enabled`` and passing the new value
        in the same call so a paused-then-resumed schedule lands atomically.

        Unknown fields raise ValueError to catch typos at the boundary
        rather than silently no-op.
        """
        unknown = set(fields) - self._AUTOMATION_UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"Cannot update automation fields: {sorted(unknown)}")
        if not fields:
            return await self.get_automation(automation_id)

        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key == "config_override":
                params.append(json.dumps(value or {}))
                sets.append(f"{key} = ${len(params)}::jsonb")
            elif key == "expert_id":
                params.append(UUID(str(value)) if value else None)
                sets.append(f"{key} = ${len(params)}")
            else:
                params.append(value)
                sets.append(f"{key} = ${len(params)}")

        sets.append("updated_at = now()")
        params.append(UUID(automation_id))
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE automations SET {', '.join(sets)} WHERE id = ${len(params)} RETURNING *",
                *params,
            )
        return self._automation_row_to_dict(row) if row else None

    async def delete_automation(self, automation_id: str) -> bool:
        """Hard-delete. Spawned jobs survive (FK is ON DELETE SET NULL on
        ``last_job_id``); the link from job → automation lives in
        ``jobs.context->>'automation_id'`` and stays valid as an audit hint.
        """
        async with self.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM automations WHERE id = $1",
                UUID(automation_id),
            )
        return result.endswith(" 1")

    async def list_automation_runs(
        self,
        automation_id: str,
        *,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Return jobs spawned by this automation, newest first. Joins on the
        ``context->>'automation_id'`` tag the dispatcher writes at fire time.
        """
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, status, description, priority, created_at, updated_at,
                       config_name, project_id, parent_job_id
                FROM jobs
                WHERE context->>'automation_id' = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                automation_id,
                limit,
            )
        return [dict(r) for r in rows]

    # =========================================================================
    # PROJECT LOOPS (self-improvement loop control rows)
    # =========================================================================

    @staticmethod
    def _project_loop_row_to_dict(row) -> Dict[str, Any] | None:
        """Normalize a project_loops asyncpg Record into a serializable dict.

        Decodes the ``role_sequence`` and ``current_stage_jobs`` JSONB columns
        to lists (plus the 0050 campaign columns — ``campaign`` /
        ``campaign_caps`` to dicts, ``campaign_history`` to a list); everything
        else passes through. Returns None on a None row so callers can chain.
        """
        if row is None:
            return None
        out = dict(row)
        for jsonb_col in (
            "role_sequence",
            "current_stage_jobs",
            "campaign",
            "campaign_history",
            "campaign_caps",
        ):
            val = out.get(jsonb_col)
            if isinstance(val, str):
                try:
                    out[jsonb_col] = json.loads(val)
                except (TypeError, ValueError):
                    pass
        return out

    async def create_project_loop(
        self,
        *,
        project_id: str,
        owner_id: str | None,
        goal: str | None = None,
        acceptance_criteria: str | None = None,
        user_prompt: str | None = None,
        model: str | None = None,
        role_sequence: List[str] | None = None,
        max_iterations: int | None = None,
        run_until: datetime | None = None,
        max_consecutive_failures: int = 3,
        workspace_backend: str | None = None,
        scheduling: str = "standard",
        campaign_caps: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Insert a project_loop control row in 'running' status.

        ``remaining_iterations`` is seeded from ``max_iterations``. At least one
        of ``max_iterations`` / ``run_until`` must be set (DB CHECK enforces it
        too). The caller (router) validates the budget before calling.
        ``workspace_backend`` (optional) is the per-loop workspace tier override
        injected into every spawned job's config_override (mirrors ``model``).
        ``scheduling`` / ``campaign_caps`` (0050) opt the loop into
        campaign-mode scheduling; both are start-time-only and validated by the
        router (docs/features/loop_campaign_scheduling.md).
        """
        roles = role_sequence or ["scholar", "critic", "developer"]
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO project_loops (
                    project_id, owner_id, status,
                    goal, acceptance_criteria, user_prompt, model,
                    role_sequence, seq_index,
                    max_iterations, remaining_iterations, run_until,
                    max_consecutive_failures, workspace_backend,
                    scheduling, campaign_caps
                )
                VALUES (
                    $1, $2, 'running',
                    $3, $4, $5, $6,
                    $7::jsonb, 0,
                    $8, $8, $9,
                    $10, $11,
                    $12, $13::jsonb
                )
                RETURNING *
                """,
                UUID(project_id),
                UUID(owner_id) if owner_id else None,
                goal,
                acceptance_criteria,
                user_prompt,
                model,
                json.dumps(roles),
                max_iterations,
                run_until,
                max_consecutive_failures,
                workspace_backend,
                scheduling,
                json.dumps(campaign_caps) if campaign_caps is not None else None,
            )
        return self._project_loop_row_to_dict(row)

    async def get_project_loop(self, loop_id: str) -> Dict[str, Any] | None:
        """Fetch one project loop by id. Returns None if not found."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM project_loops WHERE id = $1",
                UUID(loop_id),
            )
        return self._project_loop_row_to_dict(row)

    async def get_active_project_loop(self, project_id: str) -> Dict[str, Any] | None:
        """Return the project's active (running|paused) loop, or None.

        Backs the one-active-loop-per-project rule (also enforced by the
        partial unique index) and the GET endpoint.
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM project_loops
                WHERE project_id = $1 AND status IN ('running', 'paused')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                UUID(project_id),
            )
        return self._project_loop_row_to_dict(row)

    async def list_project_loops(
        self,
        *,
        project_id: str | None = None,
        owner_id: str | None = None,
    ) -> List[Dict[str, Any]]:
        """List project loops, newest first. Filters are additive."""
        clauses: list[str] = []
        params: list[Any] = []
        if project_id is not None:
            params.append(UUID(project_id))
            clauses.append(f"project_id = ${len(params)}")
        if owner_id is not None:
            params.append(UUID(owner_id))
            clauses.append(f"owner_id = ${len(params)}")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        async with self.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM project_loops {where} ORDER BY created_at DESC",
                *params,
            )
        return [self._project_loop_row_to_dict(r) for r in rows]

    # Fields the loop controller / API may mutate. id / project_id / owner_id /
    # created_at are immutable; everything else is loop runtime state.
    _PROJECT_LOOP_UPDATABLE_FIELDS = frozenset(
        {
            "status",
            "goal",
            "acceptance_criteria",
            "user_prompt",
            "model",
            "role_sequence",
            "seq_index",
            "max_iterations",
            "remaining_iterations",
            "run_until",
            "max_consecutive_failures",
            "workspace_backend",
            "current_job_id",
            "current_stage_jobs",
            "total_jobs_run",
            "consecutive_failures",
            "last_error",
            "stop_reason",
            # Planner-mode campaign state (0050). `scheduling` / `campaign_caps`
            # are deliberately NOT updatable — start-time-only (flipping a live
            # loop's mode would orphan in-flight campaign state).
            "campaign",
            "campaign_history",
        }
    )

    async def update_project_loop(
        self,
        loop_id: str,
        **fields: Any,
    ) -> Dict[str, Any] | None:
        """Partial update of a project_loop row.

        Unknown fields raise ValueError to catch typos at the boundary rather
        than silently no-op. ``role_sequence`` / ``current_stage_jobs`` /
        ``campaign_history`` are JSONB-encoded lists; ``campaign`` is a
        JSONB-encoded object where None means SQL NULL (no active campaign);
        ``current_job_id`` is coerced to UUID (None clears it).
        """
        unknown = set(fields) - self._PROJECT_LOOP_UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"Cannot update project_loop fields: {sorted(unknown)}")
        if not fields:
            return await self.get_project_loop(loop_id)

        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key in ("role_sequence", "current_stage_jobs", "campaign_history"):
                params.append(json.dumps(value or []))
                sets.append(f"{key} = ${len(params)}::jsonb")
            elif key == "campaign":
                params.append(json.dumps(value) if value is not None else None)
                sets.append(f"{key} = ${len(params)}::jsonb")
            elif key == "current_job_id":
                params.append(UUID(value) if value else None)
                sets.append(f"{key} = ${len(params)}")
            else:
                params.append(value)
                sets.append(f"{key} = ${len(params)}")

        sets.append("updated_at = now()")
        params.append(UUID(loop_id))
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE project_loops SET {', '.join(sets)} "
                f"WHERE id = ${len(params)} RETURNING *",
                *params,
            )
        return self._project_loop_row_to_dict(row) if row else None

    async def list_project_loop_jobs(
        self,
        loop_id: str,
        *,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Jobs spawned by this loop, newest first.

        Joins on the ``context->>'loop_id'`` tag the controller stamps at spawn
        time (mirrors ``list_automation_runs``).
        """
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, status, description, config_name, priority,
                       created_at, updated_at, project_id
                FROM jobs
                WHERE context->>'loop_id' = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                loop_id,
                limit,
            )
        return [dict(r) for r in rows]

    async def list_running_project_loops(self) -> List[Dict[str, Any]]:
        """All loops in 'running' status, oldest-touched first.

        Used by the safety-net sweeper to find loops whose current job went
        terminal without the completion hook advancing them.
        """
        async with self.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM project_loops WHERE status = 'running' "
                "ORDER BY updated_at ASC"
            )
        return [self._project_loop_row_to_dict(r) for r in rows]

    async def claim_project_loop_stage_barrier(
        self, loop_id: str, member_job_id: str
    ) -> bool:
        """Atomically claim the rotate for a parallel stage's LAST finisher.

        The barrier for parallel (fan-out) stages. Drains ``current_stage_jobs``
        to ``'[]'`` — and returns True to exactly one caller — iff *every* member
        of the stage is now terminal and ``member_job_id`` is still one of them.
        Called by each member's completion hook after that member goes terminal;
        the member that finishes LAST (making the "no member still running"
        predicate true) and commits first wins the rotate. Everyone else — an
        earlier finisher (a later member still runs → predicate false), a
        simultaneous co-last finisher (loses the row-level race → the set is
        already ``[]``), or a stray/re-delivered hook after rotation (the set no
        longer contains it) — matches no row and backs off.

        Membership is immutable for the stage's life (this is the only writer
        that empties it, in one shot, also nulling the width-1 display mirror
        ``current_job_id``), so the post-rotate state is the same single
        signature the torn-advance sweeper reasons about:
        ``current_job_id IS NULL AND current_stage_jobs = '[]'``.
        """
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE project_loops
                SET current_stage_jobs = '[]'::jsonb,
                    current_job_id = NULL,
                    updated_at = now()
                WHERE id = $1
                  AND status = 'running'
                  AND jsonb_array_length(current_stage_jobs) > 0
                  AND current_stage_jobs @> to_jsonb($2::text)
                  AND NOT EXISTS (
                      SELECT 1 FROM jobs j
                      WHERE j.id::text IN (
                          SELECT jsonb_array_elements_text(
                              project_loops.current_stage_jobs
                          )
                      )
                      AND j.status NOT IN ('completed', 'failed', 'cancelled')
                  )
                """,
                UUID(loop_id),
                str(member_job_id),
            )
        return result.endswith(" 1")

    async def get_loop_stage_member_statuses(
        self, job_ids: List[str]
    ) -> Dict[str, str]:
        """Map each given job id → its status (missing rows omitted).

        Used by the parallel-stage last finisher to aggregate the stage
        outcome (a stage counts as a failure only when *all* members failed;
        one success resets the consecutive-failure counter).
        """
        if not job_ids:
            return {}
        async with self.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, status FROM jobs WHERE id = ANY($1::uuid[])",
                [UUID(j) for j in job_ids],
            )
        return {str(r["id"]): r["status"] for r in rows}

    async def get_newest_loop_stage(self, loop_id: str) -> List[Dict[str, Any]]:
        """Return the newest stage's jobs (id, status, decoded context).

        A stage = all of the loop's jobs that share the maximum
        ``loop_iteration`` — members of one fan-out stage share it (spawn stamps
        the post-stage cumulative count on every member), and a single-role
        stage is a group of one. The torn-advance sweeper uses this to
        reconstruct the in-flight stage of a loop whose write-back was lost:
        one member's context carries the counter stamps, and the members'
        statuses say whether the stage is still running (restore the barrier) or
        fully terminal (the rotate was lost — re-point + advance). Ordered
        oldest-first for stable representative selection.
        """
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, status, context
                FROM jobs
                WHERE context->>'loop_id' = $1
                  AND context->>'loop_iteration' = (
                      SELECT context->>'loop_iteration'
                      FROM jobs
                      WHERE context->>'loop_id' = $1
                      ORDER BY created_at DESC
                      LIMIT 1
                  )
                ORDER BY created_at ASC
                """,
                loop_id,
            )
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            c = d.get("context")
            if isinstance(c, str):
                try:
                    d["context"] = json.loads(c)
                except (TypeError, ValueError):
                    d["context"] = {}
            out.append(d)
        return out

    async def heal_project_loop_stage(
        self,
        loop_id: str,
        member_job_ids: List[str],
        *,
        current_job_id: str | None,
        seq_index: int,
        total_jobs_run: int,
        remaining_iterations: int | None,
        min_wedge_age_seconds: float = 600.0,
    ) -> bool:
        """Restore a torn turn's in-flight membership (width 1 included).

        The torn-advance recovery (docs/issues/loop_advance_nonatomic_wedges_loop.md):
        an advance that spawned the next turn's jobs — or drained the barrier —
        but lost its write-back leaves the loop wedged with
        ``current_job_id IS NULL AND current_stage_jobs='[]'``. This re-points
        ``current_stage_jobs`` at the members so the barrier can fire,
        restores the width-1 display mirror (``current_job_id`` — pass None
        for a fan-out turn), and reconciles the counters the lost write-back
        would have set.

        Guarded on ``current_job_id IS NULL AND status='running'`` and an
        empty stage set so concurrent sweeper replicas heal exactly once —
        the loser matches no row and backs off — AND on the wedge being at
        least ``min_wedge_age_seconds`` old on the DB clock. The age gate is
        what separates a *torn* advance from an advance *in flight*: every
        healthy advance also traverses the both-cleared state between its
        barrier claim and its write-back, and healing inside that window
        re-arms the claim and double-spawns the next turn (observed live:
        duplicate iter-14 critics).
        """
        async with self.acquire() as conn:
            result = await conn.execute(
                "UPDATE project_loops SET current_stage_jobs = $2::jsonb, "
                "current_job_id = $3, seq_index = $4, total_jobs_run = $5, "
                "remaining_iterations = $6, updated_at = now() "
                "WHERE id = $1 AND current_job_id IS NULL AND status = 'running' "
                "AND current_stage_jobs = '[]'::jsonb "
                "AND updated_at < now() - make_interval(secs => $7)",
                UUID(loop_id),
                json.dumps([str(j) for j in member_job_ids]),
                UUID(current_job_id) if current_job_id else None,
                seq_index,
                total_jobs_run,
                remaining_iterations,
                float(min_wedge_age_seconds),
            )
        return result.endswith(" 1")

    async def clear_project_loop_ghost_stage(
        self, loop_id: str, member_job_ids: List[str]
    ) -> bool:
        """Release a turn whose EVERY member row has been deleted (ghosts).

        The barrier claim can never fire for such a turn — its predicate
        needs at least one existing member row — so the loop would sit
        wedged forever. Clearing both pointer columns restores the single
        torn-advance signature the sweeper's heal recovers from (next tick
        re-points at the newest surviving stage).

        Guarded on the exact membership we read, running status, and a
        DB-authoritative re-check that no listed member exists — a stale
        sweeper read (concurrent rotate already re-pointed the loop, or a
        member that still exists) matches no row and backs off.
        """
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE project_loops
                SET current_stage_jobs = '[]'::jsonb,
                    current_job_id = NULL,
                    updated_at = now()
                WHERE id = $1
                  AND status = 'running'
                  AND current_stage_jobs = $2::jsonb
                  AND NOT EXISTS (
                      SELECT 1 FROM jobs j
                      WHERE j.id::text IN (
                          SELECT jsonb_array_elements_text(
                              project_loops.current_stage_jobs
                          )
                      )
                  )
                """,
                UUID(loop_id),
                json.dumps([str(j) for j in member_job_ids]),
            )
        return result.endswith(" 1")

    async def adopt_project_loop_pointer_turn(self, loop_id: str, job_id: str) -> bool:
        """Transitional (pre-0063 writers): adopt a pointer-only width-1 turn
        into the barrier membership — iff the row still looks exactly like
        that legacy shape (same pointer, empty membership, running loop).

        A concurrent old-replica advance that re-points the loop between the
        sweeper's read and this write makes it a no-op, so a stale read can
        never graft a finished job into a newer turn's membership (which
        would fire the barrier under the running job and double-spawn).
        """
        async with self.acquire() as conn:
            result = await conn.execute(
                "UPDATE project_loops "
                "SET current_stage_jobs = jsonb_build_array($2::text), "
                "updated_at = now() "
                "WHERE id = $1 AND current_job_id = $3 AND status = 'running' "
                "AND current_stage_jobs = '[]'::jsonb",
                UUID(loop_id),
                str(job_id),
                UUID(job_id),
            )
        return result.endswith(" 1")

    async def fetch_next_due_cron_automation(self, conn) -> Dict[str, Any] | None:
        """Pessimistically claim the next due cron automation under SKIP LOCKED.

        Must be called inside an open transaction on ``conn``. The returned
        row is locked until the transaction commits or rolls back — the
        caller advances ``next_run_at`` via ``advance_automation_after_fire``
        (or ``skip_automation_fire`` for a catchup-window skip) before
        committing so concurrent dispatcher replicas don't re-pick the row.

        Returns None when nothing is due. The dispatcher loops on this until
        None to drain the backlog within one tick.
        """
        row = await conn.fetchrow(
            """
            SELECT *
            FROM automations
            WHERE enabled = true
              AND trigger_type = 'cron'
              AND next_run_at IS NOT NULL
              AND next_run_at <= now()
            ORDER BY next_run_at ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """
        )
        return self._automation_row_to_dict(row) if row else None

    async def advance_automation_after_fire(
        self,
        conn,
        automation_id: str,
        *,
        next_run_at: datetime,
        scheduled_for: datetime,
        job_id: str,
    ) -> None:
        """Mark an automation as fired and advance its cron state.

        Increments ``run_count`` and the daily ``fires_today_count`` counter
        (rolled when ``fires_today_date`` < CURRENT_DATE). Called inside the
        same transaction as ``fetch_next_due_cron_automation`` so the row
        stays locked through the update.
        """
        await conn.execute(
            """
            UPDATE automations
            SET next_run_at = $1,
                last_scheduled_at = $2,
                last_dispatched_at = now(),
                last_fired_at = now(),
                last_job_id = $3,
                run_count = run_count + 1,
                fires_today_count = CASE
                    WHEN fires_today_date = CURRENT_DATE
                        THEN fires_today_count + 1
                    ELSE 1
                END,
                fires_today_date = CURRENT_DATE,
                updated_at = now()
            WHERE id = $4
            """,
            next_run_at,
            scheduled_for,
            UUID(job_id),
            UUID(automation_id),
        )

    async def skip_automation_fire(
        self,
        conn,
        automation_id: str,
        *,
        next_run_at: datetime,
    ) -> None:
        """Advance ``next_run_at`` without firing a job — used when the
        scheduled time is older than ``catchup_window_seconds`` (orchestrator
        was down past the grace window). Does NOT touch ``last_fired_at`` /
        ``run_count`` so the cockpit shows the gap honestly.
        """
        await conn.execute(
            """
            UPDATE automations
            SET next_run_at = $1,
                last_dispatched_at = now(),
                updated_at = now()
            WHERE id = $2
            """,
            next_run_at,
            UUID(automation_id),
        )

    async def auto_disable_automation(
        self,
        conn,
        automation_id: str,
        *,
        reason: str,
    ) -> None:
        """Disable an automation in response to a safety guard (today the only
        guard is ``max_fires_per_day``). Sets ``enabled=false`` and clears
        ``next_run_at`` so the dispatcher stops considering it; the row stays
        editable so the owner can fix the cron and re-enable.

        ``reason`` is logged but not persisted — surface it through a
        notification rather than a DB column to keep the schema tidy.
        """
        logger.warning("Auto-disabling automation %s: %s", automation_id, reason)
        await conn.execute(
            """
            UPDATE automations
            SET enabled = false,
                next_run_at = NULL,
                updated_at = now()
            WHERE id = $1
            """,
            UUID(automation_id),
        )

    @staticmethod
    def _automation_row_to_dict(row) -> Dict[str, Any] | None:
        """Normalize an asyncpg ``Record`` into a JSON-serializable dict.

        Decodes JSONB columns to dicts; everything else passes through.
        Returns None on a None row so callers can chain ``or None``.
        """
        if row is None:
            return None
        out = dict(row)
        for jsonb_field in ("config_override", "event_filter"):
            value = out.get(jsonb_field)
            if isinstance(value, str):
                try:
                    out[jsonb_field] = json.loads(value)
                except (TypeError, ValueError):
                    pass
        return out

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
